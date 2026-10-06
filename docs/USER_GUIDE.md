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
80,000 characters, enough for a whole master; a longer history loses its longest
entries' last notes first, every entry stays listed, and `tailor_report.txt` carries an
advisory) plus the seed, then a second pass rewrites the draft for rhythm: varied
sentence and paragraph lengths, and any sentence that reads like a bullet with a subject
bolted on retold as a story. Two structural checks always run, whatever the **"Strip AI
writing patterns"** toggle says: a seven-word run copied straight from a résumé bullet
("bullet echo"), and sentences or paragraphs that all sit within a narrow band of the
average length ("uniform rhythm"). Settings → Resume's **"Strip AI writing patterns from
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

**When Google Drive is not running.** Your main job list lives in your Google Drive folder.
If Google Drive for desktop stops, its drive letter disappears and that list with it. The
dashboard keeps a copy of the list on this PC, refreshed each time it loads cleanly, and
shows that copy while Drive is away. An amber banner at the top names the file, says how
old the copy is and how many jobs it holds, and offers **Start Google Drive**; the status
bar adds "Google Drive offline". Once Drive is running again the dashboard switches back
by itself within about 15 seconds. Jobs you mark seen meanwhile are kept on this PC and
carry over. A Drive folder that is there without the job list (a fresh setup) shows
nothing from the copy.

Right-click any job to work with it: **Set status →** marks it applied / interviewing /
rejected / offer from any tab, and the menu also offers **Delete job** (any row) and
**Edit job…** (for jobs you added by hand). An **Add job by hand** button (High Score /
All Jobs toolbar) saves a posting you found yourself and tailors your résumé for it at once
(see *Add a job by hand* below). A **Find new jobs** button (bottom action bar)
kicks off a fresh discovery + score on demand; it asks first (a *small test run* or a
*full run*) because finding jobs costs real money / API credits.

The **Tracker** tab has **Export tracker… / Import tracker…** buttons. Your whole
application history (seen-state, statuses, and tailored-résumé links) lives in a local
SQLite file, so export a backup and import it on another machine. Import **merges** (a
more recent status wins; nothing is deleted). The **Stats** tab shows a **freshness
badge** (mirrored in the window's header strip): green when the latest pipeline run is recent, amber *"the cloud job search may
have failed"* once it's older than the **Flag data as stale after (hours)** setting
(default 36), so a broken cron run doesn't go unnoticed.

### Add a job by hand
For a posting the automatic search missed, click **Add job by hand** on the High Score or
All Jobs toolbar. Fill in all four boxes: the posting's URL, the job title, the company and
the full job description pasted from the posting. Then click **Add and tailor**. The job is
saved and the résumé tailor starts on it right away; it asks first whether you also want a
cover letter, as any tailor run does.

A job you add by hand is never scored. You picked it, so it skips the scorer (and its cost)
and always shows on High Score whatever your score filters say. Its **Score** cell reads
"hand-added".

If the URL is one you added before, the dashboard says so ("Already added on <date> as
<title> at <company>.") and offers **Tailor again** or **Cancel**. **Tailor again** runs the
tailor on the saved job with its saved description, or with the one you just pasted when
the saved one is blank. If tailoring fails, the job
stays saved and the status bar says so; run **Tailor résumé** on the job to try again. If you
run job discovery on a VM, a hand-added job is sent to the VM's job list the way the
dashboard's other new jobs are.

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

**Finding one setting among about a hundred.** Three things at the top of the tab, in this order:

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
- **Show advanced settings:** off by default, folding 23 power-user rows away on a fresh
  install (the per-stage model pickers and fallback lists, scorer concurrency and retry caps,
  Jev's three area switches, the Jev writer and the Auto-apply judge) and 26 once VM features
  are on, which
  adds the VM plumbing. The label counts what it is
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

The sections, in the order the tab shows them:

- **Jev:** the **Use Jev** switch and the **TypeSafe API key (Jev judge)** box, first
  because they decide how scoring, tailoring and auto-apply run (see *Jev* below). Under
  *Show advanced settings* sit three more switches, one per area: **Jev for scoring**, **Jev
  for the résumé tailor** and **Jev difficulty check**.
- **Credentials:** the job-data (Bright Data) API token, the Gemini API-key pool,
  and the résumé-tailor API key. Each box holds the saved value (read straight from
  your local `.env`), masked by default. Untick *Hide* to reveal one, edit it to
  change it, or clear the box to remove the key.
- **Connection & paths:** the job-postings dataset ID, Google Cloud project +
  location, your name (for résumé filenames), the résumé output folder and
  `pdflatex` path (with **Browse…** buttons), and which Chrome profile to open
  links in.
- **Résumé tailor:** its first row is the tailor's **provider** (Gemini or Claude), the
  model that writes every bullet and the cover letter. Then, on Gemini, which backend it
  bills: your Vertex project, one API key, or **pool**, the scorer's free keys with Vertex
  as the spillover (see *Tailoring on the scorer's free keys* below). See the Claude backend
  note below.
- **About you** (the tab shows it between Dashboard and Job discovery): your school status,
  graduation month and security clearance, which the scorer reads (see *About you: school
  status and clearance* below).
- **Dashboard / Job discovery / Scoring / Resume:** scores, follow-up days, search
  keywords, remote types, spend caps, artifact toggles, and more. **Drop Easy Apply jobs
  before scoring** (off by default) discards LinkedIn Easy-Apply postings before they cost
  a scoring call, for anyone who only wants postings with a real application form.
  **Repost score reuse window (days)** (Scoring, under *Show advanced settings*; default
  30, 0 turns it off) copies a still-fresh master row's score onto a new posting that
  matches on title, company, location and the first 400 characters of the description, so
  a repost does not spend a fresh Gemini call every time it resurfaces. **Jev writer for
  high scores** (Scoring, under *Show advanced settings*; on by default) is the switch for
  the quick rewrite described under *Jev* below; turn it off to keep Jev's own
  code-written reason, strengths and gaps. The first row of
  Scoring is the **Scoring provider**, which scores jobs while Jev is off and takes over
  when Jev is down.
- **Auto-apply / Settings history:** the batch-apply queue cap, which webmail inbox the
  run opens for verification emails, and how long a run waits for your answer (see
  *Auto-apply* below); plus a snapshot of your
  settings on every Save, restorable from **Restore from archive…**. **Settings
  snapshots** is one dropdown: *Off*, *Keep everything* (the default; nothing is ever
  deleted), *Keep newest 20*, or *Keep newest 100*. Each snapshot holds a copy of your
  `.env`, so more snapshots means more copies of your keys on this PC.
- **VM (cloud job discovery):** an **Enable VM features** master toggle (off by default)
  plus the non-secret connection details for your GCP job-discovery VM (instance, zone,
  project, Linux user). Off hides the whole VM area and silences VM prompts; turn
  it on to reveal the controls (see *Manage the VM* below).

The model rows are **editable dropdowns**: the scorer's two stages sit in Scoring and the
résumé tailor's in Résumé tailor. A Gemini row offers the recent Gemini 3.x ids and a Claude
row the Claude ids; each shows only while its provider is selected. Pick one or type a custom id. The
tailor asks one question before the rest, **simple or per stage**, described under *One
model for every step* below.

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
> `claude-sonnet-5`, deep → `claude-opus-5-5` (Opus 5.5). Sonnet 5.5
> (`claude-sonnet-5-5`) is one pick away in every Claude model dropdown. On a CLI too old
> for it, runs switch to `claude-sonnet-5` by themselves, and `claude update` brings it
> back. The cloud VM always scores with Gemini, regardless of this setting.
>
> Opus 5.5 needs Claude CLI version 2.1.280 or newer. On an older CLI the tailor switches
> to `claude-opus-5` by itself and goes on. The first run that hits it carries a warning in
> its `tailor_report.txt` (the dashboard's batch summary shows it), and later runs carry a
> note. Run `claude update`, then close and reopen
> the dashboard, to get Opus 5.5 back. **Check setup** names the version gap too.
>
> **Claude thinking effort** (under *Show advanced settings*) sets how long Claude thinks
> before each tailoring answer. It defaults to `low`. Each answer's time limit grows with the
> level, so a higher one has room to finish before the "Claude CLI timed out after 2
> attempts" error. Writing 16 bullets on Opus 5.5 took:
>
> | Effort | Time | Time limit per try |
> |---|---|---|
> | `low` | 21 s | 3 min, then 5 min |
> | `medium` | 27 s | 4 min, then 7 min |
> | `high` | 42 s | 5 min, then 10 min |
> | `xhigh` | 72 s | 7 min, then 15 min |
> | `max` | about 10 min | 20 min, then 30 min |
>
> A tailoring run makes several of these calls, so the whole run slows down by about the
> same factor, and a higher level also uses more of your Claude plan's usage (`max` wrote
> about 44 times the tokens of `low`). `default` sends no level and lets the CLI decide.
> To set your own limits, put them in `RESUME_TAILOR_CLAUDE_TIMEOUTS` in `.env` (for
> example `600,1200`); that list wins at any level. Close and reopen the dashboard after
> changing either.
>
> **The cover letter's own model.** With the models on `tiers`, *Show advanced settings*
> also shows **Claude model: cover letter** (**Tailor model: cover letter** on Gemini) and
> **Claude thinking effort: cover letter**. The model writes the letter's draft and every
> pass that edits it, so `claude-opus-5-5` with the effort at `high` writes the whole letter
> on Opus 5.5 at high effort while the résumé keeps its own models and effort. Blank and
> `same` (the defaults) keep the usual split: the deep model drafts, the standard one edits,
> at **Claude thinking effort**. Close and reopen the dashboard after changing them.

#### One model for every step, or one per stage
Settings → Résumé tailor, **Tailor models: simple or per stage** (and, on the Claude provider,
**Claude models: simple or per stage**). Tailoring runs in stages, and by default each one
gets its own model: a cheap one for the small calls (entry briefs, verb
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
Settings → Résumé tailor, **Resume tailor engine**. `vertex` (the default) bills every tailor call to
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
Settings → Resume, on by default. It adds a second, stricter style pass to the **cover
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
Settings → Resume. This is the sweep, the last of the bullet passes: **on by default**, and it
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

#### About you: school status and clearance
The scorer needs to know who is applying: whether you are in school, when you graduate and
which security clearance you hold. Settings → **About you** has four rows, saved to
`scoring_config.json`:

| Row | Choices | Default |
|---|---|---|
| **School status** | *Finished school*, *In school: undergraduate*, *In school: graduate* (a master's or PhD program) | *Finished school* |
| **Graduation month** | a month name or its abbreviation plus a four-digit year, such as `May 2026` or `Sept. 2027`; blank leaves the date out | `May 2026` |
| **Security clearance held** | *None*, *Public Trust*, *Secret*, *Top Secret*, *TS/SCI* | *None* |
| **Open to getting a clearance through the employer** | a checkbox | unticked |

A fresh install, and a VM with no `scoring_config.json`, use the defaults. Every scoring run
prints your profile before it scores, with a `Note:` line beneath it when a setting was
ignored or adjusted. For example:

```
Candidate: In school: undergraduate (expected May 2027); clearance: Secret, open to sponsorship
```

**If you have finished school:**

- A title that names an internship or co-op is dropped before scoring, so it costs no
  scoring call. A title for the person who runs the program, such as "Internship Program
  Manager", stays. The run prints how many titles it dropped.
- A posting that is an internship or co-op for current students (one whose title the filter
  missed), that requires current enrollment ("currently pursuing a degree"), or that limits
  applicants to a graduation window your graduation month falls outside, scores 1 on the
  1-5 scale, in the LLM scorer and in Jev (see *Jev* below). The window rule applies while a
  graduation month is set.

**If you are in school (undergraduate or graduate):**

- Internships and co-ops for your degree level are kept and scored like any entry-level role.
- A posting open only to the other degree level scores 1: a graduate-only posting for an
  undergraduate, an undergraduate-only posting for a graduate student.
- A posting limited to a graduation window your graduation month falls outside scores 1,
  while a graduation month is set.
- With **In school: graduate**, a posting that asks for a master's or PhD is kept. The
  mechanical filter that drops postings requiring an advanced degree is off, and Jev does
  not cap those postings for the degree. Graduate internships state their enrollment rule
  in exactly that wording ("currently enrolled in a Master's or PhD program").
  **Finished** and **In school: undergraduate** keep the filter.
- The scraper's defaults are Job type *Full-time* and Experience level *Entry level*, so it
  collects few internships, whatever the scorer keeps. To collect them, choose *Internship*
  under Settings → Job discovery → **Job type** and **Experience level**. Each of the two settings
  holds one value, so this replaces *Full-time* and *Entry level* until you change them back.

For every status, Jev's not-eligible check applies only when the posting carries a student
or graduate cue, such as *student*, *enrolled* or *graduation*.

**Rollover.** An in-school status whose graduation month has passed counts as finished
school. With **In school: undergraduate** and `May 2027`, you count as a student through May
2027, and from June 2027 the scorer treats you as finished (the run prints a `Note:` line
saying so). A blank graduation month never rolls over. **Finished school** with a graduation
month still in the future ignores that month and prints a `Note:` line too. The row opens on
`May 2026`, a month that has passed, so when you pick an in-school status, also set the month
you expect to graduate. Save still saves, and adds a line to its message when an in-school
status carries a month that has passed: "Your graduation month (May 2026) has passed, so the
scorer counts you as Finished school."

**Clearance.** The clearance filter runs before scoring and drops a posting when its text
asks for a clearance you cannot cover. A dropped posting gets no score and no scoring
call: the run prints "Mechanical filter: N -> M to score", and the **Filtered** column of
the **Stats** tab counts it. Each sentence that asks for a clearance counts separately, and
one sentence you cannot cover drops the posting. A sentence that names no level counts as
Secret, and a polygraph counts as TS/SCI. A posting that says no clearance is required
stays. What stays and what drops depends on the level you hold and on the checkbox:

| You hold | Checkbox unticked | Checkbox ticked |
|---|---|---|
| *None* | Any posting that asks for a clearance is dropped. | A posting that sponsors the clearance, or asks for the ability to obtain it, stays. A posting that needs an active clearance is dropped. |
| *Public Trust*, *Secret* or *Top Secret* | A posting that needs your level or a lower one stays. A higher level is dropped. | The same, and a higher level stays when the employer sponsors it or asks you to obtain it. |
| *TS/SCI* | Nothing is dropped for a clearance. | Nothing is dropped for a clearance. |

Wording that offers a clearance includes sponsoring it, being eligible for it or able to
obtain it, willingness to undergo it, and an interim clearance. A sentence that refuses ("we
will not sponsor a clearance", "interim not accepted") cancels the offer, so that sentence
counts as needing an active clearance. Visa and work-authorization sponsorship never count
for or against the clearance: "no visa sponsorship" beside "able to obtain a Secret
clearance" leaves the clearance obtainable.

When you hold a clearance or tick the checkbox, the scoring prompts state it as well, which
catches wording the filter misses. A job that needs an active clearance you do not hold
scores 1, and with the checkbox ticked a clearance the employer sponsors or asks you to
obtain is never counted as a gap. With clearance None and the checkbox unticked, the
prompts leave clearance out and the filter does this work alone. Jev reads your clearance
in its own fit question, and a clearance you do not hold puts a posting at its lowest fit.

**If you use the VM.** The VM scores with Gemini from its own copy of your scoring settings
and its own copy of `score_jobs.py`. On the VM, that script is the only one that reads the
About you rows, for the filters and the Gemini prompts. After you save a change to an About
you row, the dashboard's **Push config to VM?** prompt (VM features on) offers to push
`scoring_config.json`, and it adds a note with the upload command:

```
gcloud compute scp pipeline/score_jobs.py <user>@<vm>:~ --zone=<zone>
```

Upload `score_jobs.py` once. There is no automated code push, and until it is uploaded the
VM keeps scoring with the defaults, whatever `scoring_config.json` says. `jev_score.py` is
optional there: the VM has no `local/jev.py` or `local/config.json`, so it scores with Gemini
either way.

#### Repair reposts that reused a score
**Repost score reuse window (days)** (Scoring, under *Show advanced settings*) copies the
score of a master row that matches and is still fresh onto a new posting. Reposts reused
from 2026-09-19 until v2.0.0, which added this command, carried only the copied score,
with a blank reason, deep score, strengths, gaps and recommendation. This command fills
them in. It makes no scrape, no scoring call and no model call, and it prints counts only:

```
python pipeline/score_jobs.py --heal-reused --dry-run
python pipeline/score_jobs.py --heal-reused
```

`--dry-run` prints "Would heal ..." lines and writes nothing, so run it first. The second
command copies the reason, deep score, strengths, gaps and recommendation from the master row
named by `score_reused_from` into the blank cells of each reused row, and never replaces a
cell that has content. It heals your local run files (`*_scored.csv` and
`*_scored.csv.gz` in the run folders) first, then the master, and prints one count line for
each ("Healed N reused rows in M run files", "Healed N reused rows in the master"). A second
pass changes nothing.

Run it while no scrape or scoring run is going. It takes no lock against a run that writes
the same files, so an overlap can lose one side's write. On the VM, run it outside the cron
window.

New reposts carry all six score columns from the start. Every scoring run also heals the
master's reused rows after it saves its scored file, so the VM's master repairs itself once it
has the new `score_jobs.py`. The command is how you repair the run files on this PC.

### Jev
Jev is TypeSafe's "System One" model. It writes no text. It answers typed questions (yes
or no, which of these, how sure) with a probability, fast and for very little money: $0.042
per million input tokens, with nothing charged for its answers. The app asks Jev for the
decisions, and Gemini or Claude writes the text.

Jev works in four places:

- **Scoring.** Jev scores each collected job against your résumé on the scorer's own
  scales, in both stages: the stage 1 score (1-5), then the stage 2 deep score (1-10)
  and recommendation. The stage 1 reason names what decided the score, such as the
  candidate's skills and field, the years the job asks for, or a hard requirement like
  a clearance or an advanced degree. A job Jev scores in stage 2 gets a deep score from
  7 to 10, so it always reads apply or consider, never skip. For a job at or above the
  stage 2 threshold (4 by default), one call to your **Scoring provider**'s quick model
  writes that job's reason, strengths and gaps from Jev's findings. On the Claude
  provider each call takes about 30 to 100 seconds and costs about $0.04 at Haiku list
  prices. Turn this off at Settings → Scoring → **Jev writer for high scores**.
- **The résumé tailor.** Jev picks skills and experience items and checks each bullet,
  and your **Resume tailor provider** writes every bullet and the cover letter (see *What
  Jev does in the tailor* below).
- **The difficulty check** on the Auto-apply tab (see *How hard is each application?*
  below).
- **Auto-apply**, which reads every form page through Jev.

**Turning it on or off.** Settings → Jev → **Use Jev** is on by default. Jev also needs a
key in **TypeSafe API key (Jev judge)** (create one at `console.typesafe.ai/keys`) and the
`typesafe-sdk` package (**Check setup** says when it is missing). Under *Show advanced
settings* the same section has one switch per area: **Jev for scoring**, **Jev for the
résumé tailor** and **Jev difficulty check**, all on. A change takes effect on the next run.

**When Jev is off or down, nothing stops.** Scoring falls back to your **Scoring
provider** and the tailor to your **Resume tailor provider**, the same Gemini or Claude
setup the app used before Jev. That happens by itself when the switch is off, when there
is no key, when the package is missing, and when Jev stops answering in the middle of a
run: it retries briefly, then the rest of that run carries on without it. The one
exception is auto-apply, which has no fallback: while Jev cannot run, **Start auto-apply
run** stays greyed out with the fix beside it, such as "Auto-apply runs on Jev. Turn Jev
on in Settings > Jev." The cloud VM always scores with Gemini.

After a scoring run, the summary line says how many jobs Jev scored in each stage, what
it cost, how many fell back to the scoring provider, and how many the writer rewrote
versus left as Jev's own text.

#### What Jev does in the tailor
Six steps use Jev while it is on for the tailor. Four run on every tailor; the verb step
runs only when two bullets open with the same verb, and the sweep gate only while **Strip
AI writing patterns from the résumé bullets** is on. Each one keeps the tailor's own way
of doing that step as its fallback:

- **Skills:** Jev picks which of your skills lead each skills line for this job.
- **Shortlist:** Jev rates how well each item in your experience file fits the job, and
  the tailor chooses from the best-fitting ones.
- **Lead:** for each project, Jev picks which bullet opens it.
- **Faithfulness:** Jev checks every rewritten bullet against the items it came from. A
  bullet that claims more than you wrote, or something you never wrote, gets one retry
  from the same items; if the retry still fails, the tailor goes back to the earlier
  wording or drops the bullet.
- **Verb:** when two bullets open with the same verb, Jev picks a fresh one from the
  verb list.
- **Sweep gate:** Jev reads each entry for the AI-writing tells before the sweep (see
  above), so the sweep calls the writing model only for an entry that has one or that
  the sweep's own checks flag, and tells the model which tells it found.

Three more are switches in Settings → Resume, off by default, shown while **Jev for the
résumé tailor** is on:

- **Best of 3 bullet drafts (Jev picks):** the rewrite asks for three drafts of each
  bullet and Jev keeps the one that shows the most of what the job asks for. That call's
  model output costs about three times as much.
- **Jev checks the cover letter's claims:** Jev reads each sentence of the cover letter
  against your experience, and a sentence that claims something your experience does not
  say goes back for repair.
- **ATS report: coverage by meaning (Jev):** `ats_report.txt` gains a line counting the
  job's keywords your résumé shows in words or by a direct equivalent, beside the
  exact-match count, and a list of the ones it shows by meaning only.

`tailor_report.txt` ends with a **jev** section: one line per step, with its requests,
tokens and cost.

### What leaves your machine
There is no analytics, no crash reporting, and no phone-home. The only outbound
traffic is the work you asked for, and each destination gets only what it needs:

| Destination | When | What it receives |
| --- | --- | --- |
| Bright Data | you run job discovery | your search keywords and the dataset ID |
| Google Gemini (Vertex or API key) | scoring, résumé tailoring, and auto-apply's free-text answers | the job description, your `resume.md` / `master_experience.yaml` content; in auto-apply, the form's question and an excerpt of your apply sheet for each free-text answer it drafts |
| Anthropic (`claude` CLI) | only if you set a provider to `claude` | the same prompts, through your own CLI login |
| the employer's application site | only when you run auto-apply or the difficulty check on a queued job | auto-apply: the answers from your apply sheet and answer bank, typed into the form by the auto-apply browser profile; your sign-up email and the ATS master password when the site asks you to sign in or create an account; your résumé PDF and cover letter as file uploads. It submits only when the gate in *Auto-apply (batch, Jev-judged)* passes. The difficulty check opens the posting and its Apply page and types nothing |
| TypeSafe (the Jev judge) | only while **Use Jev** is on and a key is set | scoring: the job's text and your `resume.md`; the tailor: the job's text and the parts of your experience file, skills and drafted bullets it judges (and, with the Settings options on, the cover letter's sentences and the ATS keywords); the difficulty check and auto-apply: each form page's field labels, options and visible text, the names of your facts for the field mapping. In auto-apply the values stay on your machine until the verification and grounding checks, which receive the values it typed and an excerpt of your apply sheet. When a site emails a code or a link, auto-apply reads your webmail inbox and sends the sender, subject and preview of up to 15 of the newest messages that arrived since the job started (a message whose time cannot be read is kept, so the list can hold unrelated mail), then the opened message's body (up to 2,000 characters) with its link texts and hosts, so the judge can pick the right email and link |
| your own GCP VM (`gcloud compute ssh/scp`) | only when you click a VM control in *Settings* | your search and scoring config, the ids already collected, and rows to merge; plus, only when you click **Set on VM**, the one API key you typed into that box. It runs under your own `gcloud` login |
| healthchecks.io | **opt-in, VM cron only** | a start ping and the run's exit code; no job data, no identifiers |

The healthchecks ping is a dead-man's switch: a silently failing cron run emails
you. It is off unless you set `HEALTHCHECKS_URL`
yourself (see `scripts/run_scraper.sh`); unset, `ping_hc` is a no-op.

Your API keys never cross providers: the Gemini, Bright Data and TypeSafe secrets
(every secret field in *Settings*, plus `TYPESAFE_BASE_URL`) are stripped from the
environment before the `claude` CLI is launched, the TypeSafe client always talks to
`https://api.typesafe.ai`, and nothing is written to the repo. Secrets stay in your
git-ignored `.env`. The one API key that leaves this PC is the one you hand to
**Set on VM**, and it goes to your own VM so its cron runs can authenticate.

The ATS master password lives in the Windows Credential Manager. It leaves it to be
typed into a password box on the job's own application site, or copied to the
clipboard when you ask (the copy is kept out of Windows clipboard history and the
cloud clipboard). Auto-apply types it only on an `https` page and only on the
application's own site: a plain-HTTP password page, or a move to another company's
site on the same ATS, parks the job with a plain reason. **One password is used on
every application site**, so a site that stores it badly, or a fake sign-in page on
an admitted site, exposes your account on the others. Use a password you use nowhere
else. Per-site passwords are planned.

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
   reading. On the sheet each résumé bullet is its own `- bullet` line with a blank line
   before the next, and copying a selection from the panel or the **Expand** window keeps
   the `- ` markers. Closing the panel brings the detail card back; **"I applied to this job"**
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
you. The same goes for a question naming a number of years or your current job, and for a status
list under a "Work authorization" heading; the next paragraph says which other wordings Jev reads
for you. A custom yes, no or number answer you add fills only the question it was saved for, word
for word. When a job stops on a question like that, add a custom answer with the question exactly
as the form words it, then run the job again.

**Questions in other words.** When a form asks about relocating, working on-site, work
authorization or sponsorship in words the check above does not match, such as "Are you willing to
work in the office in New York?" or "Do you require visa sponsorship? This includes needing
sponsorship for CPT, OPT or other visa types", Jev reads your saved answers, with your work
authorization statement, and fills the field only when it is sure (0.85 or more, far ahead of
every other choice) that anyone with those answers would pick that option. Otherwise the field
stops the job as before. The question itself says which answer Jev reads: "We work 5 days on-site
in NYC. If you're not local, are you willing to relocate?" asks about moving, so your relocation
answer settles it even when the office sentence comes first. For a relocating or on-site question
Jev also reads where you live now: the city and state of your confirmed mailing address, or the
location on your résumé when no address is saved. That lets it choose between options such as "I
am in NYC and happy to work in office" and "I will relocate and am happy to work in office". Some
questions always stop the job, since none of your answers says them: another country, whether you
are employed now, a visa or sponsor you hold now, your employer's needs, relocation help you ask
for, or a question only about commuting or where you live. If you need sponsorship, so do
questions about now alone, about restrictions, about one visa type such as H-1B, or with a list
of who counts as authorized, since your answers do not name your visa. A No to relocating leaves
"Are you located in or willing to relocate?" open, since you may live there already. Years of
experience and remote-only work always need their own question. The note on an answer goes to Jev
with it, so "only in NYC or Austin" or "only with relocation help" tells it what your Yes covers.

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

The older auto-apply that ran through Claude-in-Chrome (the `auto-apply` skill and its
helper scripts) has been removed; auto-apply now runs on Jev alone. The **Apply** panel's
by-hand flow with Claude-in-Chrome, above, is unchanged.

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
   `TYPESAFE_API_KEY=...`, or paste it into **Settings → Jev → TypeSafe API key (Jev
   judge)** (it loads masked; untick *Hide* to see it; a rotated key needs a dashboard restart), and leave
   **Use Jev** on. Then install the browser half once, from the project folder (README
   Step 7): `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -AutoApply` puts
   playwright==1.61.0 into the venv and its Chromium into `%LOCALAPPDATA%\ms-playwright`.
   The run uses your installed Google
   Chrome and falls back to that Chromium when Chrome is absent. **Check setup** names
   anything still missing, and `python local/apply_run.py doctor` prints the same rows
   from a terminal.
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
portal forces one, fetches an emailed code or link from your inbox (never one sent by LinkedIn, your mail
provider, a sign-in service or another platform than the job's), and clicks the button
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
  The run first pauses and asks you (see *When the run needs you* below); it parks the job
  when you do not answer in time, when you choose **Park it**, and in the other cases
  *What ends a pause other than an answer* lists. For a parked job, click
  **Answer now**: it opens **Add answer** on the **Apply Answers** tab with the question
  filled in, and once you save it offers to **Re-queue** the job. A saved yes/no or number
  answer fills a later application only where that job words the question the same way. A
  way on that stays disabled until a field the run left blank is answered stops the same
  way. An optional question with no answer is left blank.
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
- A safety stop on the site itself: a page that moves the job to another company's account
  or to another application platform ("the application left the job's own account on its
  application platform"), a password box on a page that is not `https` ("this site asks
  for a password over an unencrypted connection"), or a page with more than 200 boxes
  ("the page holds more boxes than the run reads on one page"). A career-site front end
  such as Eightfold or Phenom may hand the job on to the company's own platform; the
  first platform the job reaches is its account from then on.
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
hand and **Mark applied** or **Re-queue** it. The settings under **Settings →
Auto-apply** that shape a run:

- **Submit when verified** (`auto_apply_submit`, on): off parks every job at its review
  page instead. `--no-submit` on the command line does the same for one run.
- **Draft free-text answers** (`auto_apply_generate`, on): a required open-ended
  question ("why this role", "a project you are proud of") gets one flash-lite draft
  from your apply sheet, and Jev checks each sentence against the sheet; a draft with
  an unsupported sentence is dropped and the field is left for you. An optional
  open-ended question ("What are you looking for in your next role?") is always left
  blank, since none of your saved answers holds its answer. At most three drafts per
  job. Off leaves every such field for you.
- **Hide the browser window** (`auto_apply_headless`, off): on runs Chrome with no
  window. Leave it off to watch the run and step in when it parks.
- **Wait for your answer (minutes)** (`auto_apply_pause_minutes`, 10, from 0 to 60): how
  long a run waits for you when it pauses on a question (see *When the run needs you*
  below). 0 parks the job at once, as runs did before the pause existed.
- **Auto-apply judge** (`auto_apply_jev_mode`, `typesafe`, under *Show advanced
  settings*): `fake` is a test-only judge
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

#### When the run needs you: pause and resume
Some stops are questions only you can answer. For those, the run pauses and asks you,
then carries on with your answer. It pauses for:

- a required field your saved answers cannot fill;
- two options that both match your answer equally well;
- a required field only you should type (an SSN, a birthdate, bank or card details, an ID
  number);
- a way on that stays disabled after the fill.

Every other stop in the park policy above still parks the job at once, and so does any
question after something may already have been sent.

**How you hear about it.** The run's console window prints a line and beeps: "job
<id>: waiting for you: <reason>. Answer in the dashboard's Auto-apply tab or fill it in
the browser (up to 10 min)." The job's browser tab comes to the front with the fields it
asks about outlined, and the dashboard's taskbar button flashes.

**The Waiting-for-you card.** At the top of the **Auto-apply** tab a **Waiting for you**
card shows the job and each question it asks, in a control that fits it: a short list of
options as buttons, a longer one as a dropdown, Yes or No for a tick box, a number box or a
text box. A field only you should type shows "Type this one in the browser" in place of a
box; code never types those. Then pick one of three buttons:

- **Fill and continue:** the run puts each of your answers in its own field and goes on.
- **I filled it in the browser, continue:** you typed the answers into the form yourself;
  the run reads the page again and goes on. This button is hidden when the browser window
  is hidden.
- **Park it:** the job stops as it did before the pause existed, with its usual reason.

**Save for future runs.** Beside each question sits a **Save for future runs** box, off by
default. Tick it before **Fill and continue** and the answer is also saved on the **Apply
Answers** tab as a confirmed
custom answer, with the note "Saved from <company> on <date>", so later runs fill that
question by themselves. The box is not offered for a field only you should type, and it is
greyed out with the reason when **Add answer** would refuse the question: a built-in answer
already covers it, or you already saved an answer for it. The run reads your answers again
after each pause, so a saved answer counts for the rest of the run. The answer is saved even
when the run drops your card answer because the page moved on (see below). The saved copy
then reaches a later field only where that field asks the same question as the one you
answered.

**Time limits.** A run waits **Wait for your answer (minutes)** for you, 10 minutes by
default (Settings → Auto-apply, 0 to 60; 0 turns the pause off). The wait does not count
against the job's own time limit. One job can pause at most five times.

**What ends a pause other than an answer:**

- No answer in time: the job parks with its usual reason.
- **Park it:** the same.
- You close the browser window or the job's tab while it waits, on any page. You had the
  browser and may have clicked on and sent the application before the close, so the job
  parks as possibly sent: "check whether the application went through: the run stopped
  after the pause (the browser window was closed)", or "(the job's tab was closed)", then
  what the paused page showed and what the run had reached. A wait that fails with the page
  still open (the browser stopped responding) ends the same way, with "(the job's page
  stopped answering during the wait)". It is never re-queued, and
  **Answer now** offers no **Re-queue**: check that job. A closed window also stops the
  run; a closed tab ends only that job.
- The page moved on while it waited. A new "thank you" message parks the job as possibly
  sent on any page. From a page with no send button (you clicked **Next** yourself), the
  run reads the new page and goes on, and the answers you gave in the card for the page it
  left go into no field. The run counts the page as moved on when its address changed or
  any field it had when the run paused is gone, even when the next step repeats the field
  you answered. A next step at the same address that shows every field of the paused page
  again (plus new ones) still reads as the same page, and your answer goes into the box
  with the same label there. A new request for a question you just answered means the page
  moved on during the wait (a link inside the page can change the address too): answer it again.
  From a page that could send the application, a new address or the send button or the
  form gone parks the job as possibly sent, and it is never re-queued: check that job.

A run with the window hidden pauses too, unless every question needs the browser; then it
parks, and you answer it on a run with the window shown.

#### How hard is each application? (the difficulty check)
Before you queue a batch, the difficulty check tells you which jobs the run is likely to
finish and which ones you are better off doing yourself. Select one or more jobs on the
**Auto-apply** tab (Ctrl-click or Shift-click for several; select none for every queued job)
and click **Check difficulty**. A new terminal window opens each posting in the auto-apply
browser, follows its Apply button and reads the first application page. It types nothing,
signs in nowhere and submits nothing; the Apply button is the only thing it clicks. It costs
about 2 to 4 Jev requests per job.

Each job gets a score from 1 (easy) to 10 (hard), shown in the tab's **Difficulty** column
as "N/10":

| Score | What it means |
| --- | --- |
| 1 to 3 | **Queue it**: the run should finish this one. |
| 4 to 6 | **May need an answer or two**: expect a pause or a park you can answer. |
| 7 to 10 | **Do it yourself**: apply by hand. |

The score starts from the application system (Greenhouse, Lever and Ashby are the easiest;
Workday and iCIMS harder; Taleo, SuccessFactors and Oracle the hardest), then adds for each
required question your saved answers cannot fill, each required written answer, a field
only you should type, a CAPTCHA and an account wall. Your own history counts too: a system
where past runs parked twice or more scores a point higher, and one where they reached the
end twice or more without a park a point lower. Some pages score 10 at once: an **Easy
Apply** job always scores 10, **Do it yourself** ("Easy Apply: the run leaves Easy Apply
jobs to you"), and so do a closed posting, a dead page and a payment page.

Hover over a score to see its reasons and the exact questions your answers cannot fill. A
result older than 7 days shows its age beside the score. After you save an answer on the
**Apply Answers** tab, run **Check difficulty** on the job again to see the new score.

**Check difficulty** is hidden while **Jev difficulty check** is off in Settings → Jev, and
greyed out with the reason while Jev cannot run or another browser holds the auto-apply
profile. From a terminal: `python local/apply_assess.py` with job ids, or `--all` for every
queued job (add `--parallel N`, 1 to 10, to set how many run at once for that run).

**Several jobs at once.** With two or more jobs selected, or none (every queued job), the
check opens one browser window per job, up to the **Difficulty checks at once** setting (Settings →
Auto-apply, 1 to 10, default 10; it shows only while the difficulty check is on). When a
window's job is done, the next job takes its place. Each window runs on its own temporary
copy of the auto-apply profile, so it is already signed in, and your real profile is held
for the whole run. The copies hold your session cookies. They are deleted when the run ends,
after a wait of up to about 12 seconds for a window still closing. A copy that outlives its
run (you closed the terminal with X, or a window would not let go of it) is deleted the next
time the auto-apply browser opens (a check, a run or the sign-in) or the dashboard starts,
and the terminal names any copy it could not delete. The terminal prints one line
per job as it finishes, with the running total of Jev requests and cost. A run stops
starting new jobs when Jev is down, when you close one of the windows, or when a browser
cannot start; the windows still open finish, and the terminal says why. A job whose window
fails on its own (its check crashed, or its copy was still in use) gets its "not checked"
line with the reason and the warnings behind it, and the next job starts. Ctrl+C in the
terminal closes the windows and deletes the copies. A stopped job keeps its earlier result.
One job selected, or the setting at 1, checks one job at a time on the real profile as before.

**Memory.** Each window is a full Chrome, so ten windows use several gigabytes. On a smaller
machine, lower **Difficulty checks at once**.

**With several rows selected,** **Check difficulty** checks all of them ("Checking N selected
jobs, up to K at once, in a new terminal."), and **Remove from queue** asks first, then
removes them in one step. **Re-queue**, **Mark applied**, **Don't apply** and the details
pane's **Open job folder**, **Open application record** and **Answer now** are greyed out ("Select one job"), because each
acts on a single job.

**One check or run at a time.** A run, **Sign in to sites** and the difficulty check all use
the same browser profile, so only one of them can have it open (a check of several jobs
holds the profile for its whole run, though its windows use the copies). While one does, the others
refuse to start and say "The auto-apply browser is open: a run, a sign-in or a difficulty
check holds its profile." Close that window, or wait for it to finish.
