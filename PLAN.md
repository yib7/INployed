# Job Scraper — Finish, Productionize, Ship

> Self-contained brief to drive this project. Do all work in a single continuous session, progressing in discrete stages.

## Working agreement
- **Git hygiene:** private repo; write `.gitignore` *before* the first commit; commit small, conventional units (`feat:`/`fix:`/`docs:`/`refactor:`); run `gitleaks detect` before any push and before flipping public. Private now → public when polished.
- **How to chunk for Claude:** point at file paths, never paste large files; one verifiable deliverable per stage; design in plan mode, implement sequentially; subagents only for fan-out exploration; one stage = one commit.
- **Closeout (end of project):** ① you write the "why" in your own words; ② read-only stage produces a codebase-explainer doc for your `master_experience` notes; ③ `/security-review`; ④ `CREDITS.md`; ⑤ cohesion refactor (Claude lists dead files, *you* delete); ⑥ showpiece README with screenshots; ⑦ push.

## Context
End-to-end job pipeline: GCP VM cron → Bright Data LinkedIn scrape (`scraper.py`) → two-stage Gemini scorer (`score_jobs.py`) → Google Drive → Windows Tkinter dashboard (`local/ui.py`: high-score triage, application tracker, stats) → on-demand LaTeX résumé-tailoring engine (`local/resume_tailor/`: ATS scoring, cover letters, interview prep, autofill data for Claude-in-Chrome). ~7K LOC, polished, tested. **Not a git repo yet; no `.gitignore`.** Goal: make it publishable, productionize it for any user, add a smarter master-experience flow.

## Resume framing (the "sounds tacky" worry)
Frame it as a **systems-engineering** piece, not "I cheated job apps." Lead with the engineering; the personal use is the origin story. Suggested bullet:
> Built and deployed an end-to-end job-discovery pipeline: a GCP-hosted Python scraper (Bright Data API) feeding a two-stage Gemini LLM relevance scorer, syncing to a Windows desktop dashboard (Tkinter) with application tracking and an automated LaTeX résumé-tailoring engine (ATS keyword scoring, cover-letter generation). ~7K LOC, pytest-tested, scheduled via cron + Task Scheduler.

## Security / secrets (no leak — never been pushed — keep it that way)
Before the repo exists, externalize:
- `scraper.py:10-11` — Bright Data **token + dataset id** → env (`BRIGHT_DATA_API_TOKEN`, `BRIGHT_DATA_DATASET_ID`). Rotate the token once as precaution.
- `local/ui.py:53` — `CHROME_ACCOUNT` → env/config.
- `HANDOFF.md` — replace your GCP project id, emails, VM name, service-account with placeholders (keep the doc; it's excellent).
Add `.env.example` documenting every variable.

## `.gitignore` (write before first commit)
Ignore: `resume.md`, `resume_tailor_files/master_experience.yaml`, `resume_tailor_files/resume_*.pdf`, LaTeX artifacts (`*.aux`/`*.log`/`*.synctex.gz`), `local/config.json`, `seen.db`, `*.csv.gz`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.env`. Commit a `master_experience.example.yaml` template instead of the real one.

## Productionize for any user (your `Production for Resu_Tailor.txt`)
Make resume_tailor a modular, BYO-everything local tool:
- **Two setup modes:** **Fast** = sensible defaults, minimum inputs, pipeline runs immediately. **Long** = guided, more personable/tailored (richer master-experience, preferences, templates). One code path, two entry flows.
- **Persistent setup UI/wizard:** user inputs their files (resume, master-experience, API keys) and tunes settings as they go; **state is saved** to a local config so they can revisit, edit, and add content over time. Could extend the existing Tkinter app with a Settings/Setup tab, or a `setup.ps1` that writes `config.json` + `.env`.
- **Folder reorg** so structure is self-explanatory; strip every file containing personal info or keys (move to user-supplied/templated).
- **Scraper API section:** user supplies their own Bright Data key and runs their own VM pipeline. Offer a simpler automated path — recommended default is an **on-demand "Run now / fetch latest jobs" button** (plus optional: scheduled local run, or run-at-startup). Document the VM path for advanced users.
- **Good practices + concise README** aimed at a non-expert: clear, documented, not overbearing.
- **UI Responsiveness & Performance:** Optimize the Tkinter dashboard to eliminate lag and ensure a seamless, highly responsive user experience. Explore creative options (e.g., async operations, background threads, or efficient event loops) to keep the interface fluid.

## New feature — smarter `master_experience.yaml`
On a tailored job, after extracting the JD keywords:
- For keywords/skills **not** present in the resume that are **non-identifying**, surface them to the user: "you might have this — confirm?" (catches skills they have but forgot to list).
- For confirmed/owned skills, **auto-incorporate** them into `master_experience.yaml` in the best-fit structure. Use a **flash-lite** model for this (cheap, frequent). Keep edits reviewable (diff/confirm before write).

## New feature — Resume Bullet Length Formatting (`res_tailor`)
Optimize resume space and provide the LLM with strict formatting constraints for generating bullets:
- **Single-line bullets:** Must be $\ge$ 75% of the maximum line length (in character space) to prevent useless, overly short lines (e.g., a line with only 7 words).
- **Multi-line bullets:** The final line must be $\ge$ 50% of the maximum character length for a line.
- This ensures the resume space is adequately utilized without being bloated, allowing the LLM to "breathe" when needed. 
- *Note:* Ensure the LLM uses proper LaTeX code for symbols (e.g., `\ge` or `\geq` for greater than or equal to) so they render correctly in the PDF.

## Implementation stages (each = one commit)
1. ~~Externalize secrets/identity + `.env.example` + sanitize `HANDOFF.md`.~~ **(Completed)**
2. ~~`git init` + `.gitignore` + `gitleaks` scan + first commit.~~ **(Completed)**
3. ~~Modularize for any user, Phase 1: config-driven inputs + `master_experience.example.yaml`.~~ **(Completed)** — `tailor:` yaml section drives required blocks + fixed line budgets (no hardcoded org names); header/Education render from yaml `basics`/`education`; template stripped to a generic preamble; candidate name derived from yaml. Tests in `tests/test_tailor_config.py`; resume compiles end-to-end.
4. Setup wizard (fast vs long) with persisted config + UI responsiveness optimizations. (large — isolate)
5. Smarter master_experience JD-gap feature + bullet length formatting. (medium)
6. Showpiece README + architecture diagram + setup docs. (medium)
7. Closeout checklist (security review, credits, refactor, codebase-explainer doc, push). (medium)

## Verification
`grep` finds no literal Bright Data token / personal email in tracked files • `pytest` passes (existing 36+ tests) • pipeline runs purely from env/config with no hardcoded identity • dashboard smoke test (`tests/smoke_ui.py`) passes • fresh clone + fast-setup produces a runnable tool with placeholder data.
