---
name: apply-to-job
description: Use when the user wants to fill out a job application form in Chrome from a tailored résumé's apply_data.json — fill-only, multi-page-aware form-filling for Greenhouse / Lever / Ashby / Workday / generic career pages. Fills every safe field across every page and stops at the final Submit screen for the human to review and send. Never submits, never logs in, never creates accounts.
---

# Apply to a job (Claude-in-Chrome form-filler)

Fill a job-application form from a tailored résumé's `apply_data.json`, page by page, then hand control
back to the human at the final Submit screen. This skill **fills — it never submits, never logs in,
and never creates accounts.**

## How this skill is launched (read this first)

This is a **Claude skill**, not a Google/Gemini feature. It only runs inside **Claude** (this Claude
Code session, or the Claude desktop app) **with the Claude-in-Chrome extension connected**. Nothing
happens if you "invoke it on Google" — Gemini/Google AI Studio/Chrome's address bar don't read Claude
skills. To use it:

1. **Tailor the résumé for the job first** (dashboard → "Tailor resume"), so its `apply_data.json` gets
   written next to the PDF in `~/Downloads/Generated_Resumes/<Company>/<Title>/`.
2. Make sure the **Claude-in-Chrome extension is installed and connected**.
3. **Tell Claude** "apply to the <Title> job at <Company>" (or click **Apply** in the dashboard to open
   the posting + copy the résumé path, then tell Claude to fill it). The dashboard's Apply button only
   opens the posting and copies the path — it does **not** start Claude on its own.

## Non-negotiable safety contract

1. **Never click the final Submit / Apply / Send / Finish control.** Fill every page up to the final
   submit screen, then stop and let the human review and send. Surface what's filled and what still
   needs them.
2. **Never create an account, never enter a password, never log in, never read or enter an email
   verification code.** If the site needs an account or login (very common on Workday), **stop and ask
   the human to do that step themselves**, then continue once they're in.
3. **Never enter credentials, payment info, SSN, or government IDs.**
4. **Never solve CAPTCHAs or bot-checks.** If one appears, stop and ask the human to clear it, then
   continue.
5. **Never invent an answer.** If a field isn't covered by the profile or the answer bank, leave it
   blank and add it to a "needs your input" list (see Step 4). Do not guess salaries, dates, or essays.
6. **Confirm before you start filling.** Tell the user which job/company you're about to fill and wait
   for their go-ahead — filling a form enters their personal data.
7. **One application at a time.** Do not batch-apply across postings.

## Step 1 — Load the apply context

Every tailored résumé has an `apply_data.json` next to its PDF in
`~/Downloads/Generated_Resumes/<Company>/<Title>/`. Get it one of two ways:

- **From the dashboard:** the user clicks **Apply** on a job — that opens the posting in Chrome and
  copies the résumé PDF path to the clipboard. The job's `apply_data.json` is in the folder it names.
- **From the CLI** (run from the repo's `local/` dir):
  ```
  python -m resume_tailor.apply --job-id <job_posting_id> --print
  ```
  This prints the candidate, job, apply URL, and résumé path. Read the folder's `apply_data.json` for
  the full profile.

`apply_data.json` schema you will use:

| JSON path | Use for these form fields |
|---|---|
| `candidate.full_name` / `email` / `phone` / `location` | Name, email, phone, city/location |
| `candidate.linkedin` / `github` | LinkedIn URL, GitHub/portfolio URL |
| `education[]` (`school`, `degree`, `concentration`, `gpa`, `dates`, `location`) | Education section |
| `documents.resume_pdf` | Résumé file upload (absolute path; also on the clipboard) |
| `documents.cover_letter_pdf` | Cover-letter upload (only if present/non-empty) |
| `job.title` / `job.company` / `job.url` | Sanity-check you're on the right posting |
| `resume_bullets[]` | Optional "describe your experience" fields — paraphrase, don't fabricate |
| `standard_answers.*` | Flat boilerplate answers (work auth, EEO, source) — the quick lookup |
| `answer_bank[]` | The full answer store: each `{question, answer, kind, status}` (see below) |

**`answer_bank`** is the master answer store (managed from the dashboard's **Apply Answers** tab). Each
entry has a `kind` and `status`:

- `kind: "fixed"` → use the answer **verbatim** (work authorization, sponsorship, EEO self-ID — never
  reword these).
- `kind: "open-ended"` → you may lightly adapt the answer to fit this job's phrasing, staying truthful.
- `status: "needs-review"` → a question captured on a previous run with no good answer yet. Treat it
  as not-answered (leave the field for the human) unless the user has since filled it in.

`standard_answers` defaults reflect a US citizen / green-card holder who needs no sponsorship
(work_authorized → Yes, requires_sponsorship → No, EEO → Decline to self-identify, how_did_you_hear →
LinkedIn).

## Step 2 — Get to the application form

The posting URL (`job.url`) is usually the LinkedIn listing. Click through to the real application
(the **Apply** / **Apply on company site** link), which lands on the ATS. Confirm the company/role
matches `apply_data.json` before filling anything.

If the ATS requires an **account or login** (Workday almost always does): **stop and ask the human to
log in / create the account themselves.** Do not do it for them. Once they're authenticated, continue.

## Step 3 — Fill, page by page, until the Submit screen

Detect the platform from the URL/page, then fill the visible fields. After an autofill-from-résumé
parse, **verify every parsed field** against the profile — parsers frequently mis-split names and
garble dates.

**Multi-page applications:** many forms span several pages. After filling everything on the current
page, click the **advance** control — *Next*, *Continue*, *Save and continue*, *Review* — and fill the
next page. **Repeat until you reach the page whose only remaining action is the final
Submit/Apply/Send/Finish — then STOP.** Treat "advance" controls as safe to click; treat the final
submit as forbidden (contract #1).

If advancing hits a login / account / verification / CAPTCHA wall, pause per contract #2/#4, let the
human clear it, then resume filling.

Platform notes:
- **Greenhouse** (`boards.greenhouse.io` / `job-boards.greenhouse.io`): usually one page. Upload the
  résumé first; let it autofill; correct parsed fields; answer custom questions from the answer bank.
- **Lever** (`jobs.lever.co`): one page. "Additional information" is free-text — leave blank or paste a
  short truthful line; never fabricate.
- **Ashby** (`jobs.ashbyhq.com`): multi-section single page; fill each section.
- **Workday** (`*.myworkdayjobs.com`): account-walled + multi-step. Stop for the human to authenticate,
  then fill the wizard page by page, advancing with the wizard's Next/Continue until the final review.
- **Generic career page:** best-effort. Map visible labels to the table above; flag anything ambiguous.

Use the Chrome file-upload tool to attach `documents.resume_pdf` (and the cover letter if present).

## Step 4 — Capture questions you couldn't answer

While filling, collect every question that **isn't** covered by the profile or the answer bank
(custom screening questions, essays, anything you left blank). For each, note whether it looks:

- **static** (a yes/no or short-fact the user could pre-answer once — e.g. "Do you have a security
  clearance?") → a good candidate to add to the answer bank as an **open-ended** (or fixed) entry, and
- **prompt-style** (a job-specific essay) → still worth saving so the user can write a reusable draft.

Record them so they're reusable next time:

- **If you have local file access in this session**, append them to the master store as needs-review:
  ```
  python -c "import sys; sys.path.insert(0,'local'); from resume_tailor import apply_answers as a; a.append_needs_review(['<question 1>','<question 2>'])"
  ```
  (run from the repo root; dedupes by question, marks them `status: needs-review`, `kind: open-ended`).
- **Always** also surface them in chat as a paste-able list, so the user can add/answer them in the
  dashboard's **Apply Answers** tab even if file access isn't available here.

## Step 5 — Flag, then stop

When the fields are filled and you've reached the final Submit screen:

1. List every field you **filled** (field → value).
2. List every field that **needs the human** (unmapped, essays, anything you weren't sure about, plus
   any login/account/verification/CAPTCHA you stopped for).
3. List the **new questions captured** (Step 4) and where to manage them (Apply Answers tab).
4. Remind them: **review every field, then submit yourself.** Do not click Submit.

That hand-off — every page filled, clearly annotated, submission left to the human — is the whole job.
