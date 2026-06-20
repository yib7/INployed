---
name: apply-to-job
description: Use when the user wants to fill out a job application form in Chrome from a tailored résumé's apply_data.json — semi-automated form-filling for Greenhouse / Lever / Ashby / Workday / generic career pages. Fills fields from the saved profile and standard answers, then STOPS for the human to review and submit. Never auto-submits.
---

# Apply to a job (Claude-in-Chrome form-filler)

Fill a job-application form from a tailored résumé's `apply_data.json`, then hand control back to
the human to review and submit. This skill **prepares and fills — it never submits.**

## Non-negotiable safety contract

1. **Never click the final Submit / Apply / Send control.** Fill every field, then stop and let the
   human review and submit. Surface exactly what's filled and what still needs their input.
2. **Never invent an answer.** If a field isn't covered by the profile or `standard_answers`, leave
   it blank and add it to a "needs your input" list. Do not guess salaries, dates, or essay answers.
3. **Never enter credentials, payment info, SSN, or government IDs.** If the site needs an account or
   login, stop and ask the human to log in themselves.
4. **Never solve CAPTCHAs or bot-checks.** If one appears, stop and ask the human to clear it.
5. **Confirm before you start filling.** Tell the user which job/company you're about to fill and
   wait for their go-ahead. Filling a form is entering personal data on their behalf — get a yes.
6. **One application at a time.** Do not batch-apply across many postings.

## Step 1 — Load the apply context

Every tailored résumé has an `apply_data.json` next to its PDF in
`~/Downloads/Generated_Resumes/<Company>/<Title>/`. Get it one of two ways:

- **From the dashboard:** the user clicks **Apply** on a job — that opens the posting in Chrome and
  copies the résumé PDF path to the clipboard. The job's `apply_data.json` is in the folder it names.
- **From the CLI** (run from the repo's `local/` dir):
  ```
  python -m resume_tailor.apply --job-id <job_posting_id> --print
  ```
  This prints the candidate, job, apply URL, and résumé path. Read the folder's `apply_data.json`
  for the full profile.

`apply_data.json` schema you will use:

| JSON path | Use for these form fields |
|---|---|
| `candidate.full_name` / `email` / `phone` / `location` | Name, email, phone, city/location |
| `candidate.linkedin` / `github` | LinkedIn URL, GitHub/portfolio URL |
| `education[]` (`school`, `degree`, `concentration`, `gpa`, `dates`, `location`) | Education section |
| `documents.resume_pdf` | Résumé file upload (absolute path; also on the clipboard) |
| `documents.cover_letter_pdf` | Cover-letter upload (only if present/non-empty) |
| `job.title` / `job.company` / `job.url` | Sanity-check you're on the right posting |
| `resume_bullets[]` | Optional "describe your experience" / cover-blurb fields — paraphrase, don't fabricate |
| `standard_answers.*` | The boilerplate screening questions below |

`standard_answers` (defaults reflect a US citizen / green-card holder who needs no sponsorship):

| Field | Maps to questions like |
|---|---|
| `work_authorized: true` | "Are you legally authorized to work in the US?" → **Yes** |
| `requires_sponsorship: false` | "Will you now or in the future require sponsorship?" → **No** |
| `authorization_statement` | Free-text work-authorization questions |
| `years_experience: "0"` | "Years of experience" (entry-level) |
| `willing_to_relocate: true` | "Are you willing to relocate?" |
| `gender` / `race_ethnicity` / `veteran_status` / `disability_status` | EEO self-identification → use the value (default "Decline to self-identify") |
| `how_did_you_hear` | "How did you hear about us?" → LinkedIn |

## Step 2 — Get to the application form

The posting URL (`job.url`) is usually the LinkedIn listing. Find and click through to the real
application (the **Apply** / **Apply on company site** link), which lands on the ATS. Confirm the
company/role matches `apply_data.json` before filling anything.

## Step 3 — Identify the ATS and fill

Detect the platform from the URL/page, then fill. After an autofill-from-résumé parse, **verify
every parsed field** against the profile — parsers frequently mis-split names and garble dates.

- **Greenhouse** (`boards.greenhouse.io` / `job-boards.greenhouse.io`): single page. Upload the
  résumé first; let it autofill; correct the parsed fields; answer the custom questions from
  `standard_answers`.
- **Lever** (`jobs.lever.co`): single page, similar to Greenhouse. The "Additional information" box
  is free-text — leave blank or paste a short, truthful line; never fabricate.
- **Ashby** (`jobs.ashbyhq.com`): multi-section single page; fill each section, then stop.
- **Workday** (`*.myworkdayjobs.com`): multi-step and usually **account-walled** — stop at the
  login/create-account step and ask the human to authenticate, then continue filling the wizard
  page by page. Do not create the account yourself.
- **Generic career page:** best-effort. Map visible labels to the table above; flag anything
  ambiguous rather than guessing.

Use the Chrome file-upload tool to attach `documents.resume_pdf` (and the cover letter if present).

## Step 4 — Flag, then stop

When the visible fields are filled:

1. List every field you **filled** (field → value).
2. List every field that **needs the human** (unmapped, essay/free-text, anything you weren't sure
   about, plus any login/CAPTCHA you hit).
3. Remind them: **review every field, then submit yourself.** Do not click Submit.

That hand-off — fully filled, clearly annotated, submission left to the human — is the whole job.
