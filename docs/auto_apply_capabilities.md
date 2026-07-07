# Auto-apply capability spike (SP1)

> **⚠️ GATE STATUS SUPERSEDED (2026-07-06).** The "GATE: FAIL — no upload path" verdict below was
> a limitation of the *Chrome-MCP* toolset only. A **Playwright** driver clears it:
> `page.set_input_files` attaches a plain local PDF (proven live on Greenhouse). See
> **"UPDATE (2026-07-06): GATE CLEARED"** at the bottom. The FAIL analysis is kept as the
> historical record of why the Chrome-MCP-only path couldn't upload.

Live-Chrome probe of the `mcp__claude-in-chrome__*` toolset against a real ATS application form,
run 2026-07-05 to decide whether the auto-apply agent can fill and park applications end-to-end.
Test surface: a live Lever application form (`jobs.lever.co/palantir/<id>/apply`), plus the
Windows/Chrome file-dialog and clipboard behaviours. Nothing was ever submitted.

## Verdict

**GATE: FAIL — resume/file upload has no working programmatic path.** Every other capability the
flow needs works. `mcp__claude-in-chrome__file_upload` accepts **only files the user has explicitly
shared with the session** — local resume PDFs (Downloads, the session scratchpad, *and* the repo
working directory) are all rejected — and the OS-native-dialog fallback is not drivable (Chrome is
read-tier under computer-use, and Chrome-under-automation intercepts the native file chooser so no
dialog even appears). Password paste — the other thing the GATE turns on — **works**. So this is a
single-capability failure with a clear redesign, not a dead end. See "Redesign" below; the agent can
do ~95% of each application and the resume attach is the one step that needs a design decision.

## Capability matrix (control type × tool ladder × verdict)

| Capability | Tool ladder tested | Verdict |
|---|---|---|
| Text input | `form_input(ref, "value")` | ✅ Works, first try. DOM-level (`.value` + events), coordinate-independent. |
| Email input | `form_input(ref, "value")` | ✅ Works. Tool reports `Set email value`. |
| Textarea | `form_input` / clipboard paste | ✅ Works (paste verified below). |
| Native `<select>` | `form_input(ref, "OptionText")` | ✅ Works — matched "LinkedIn" by visible text; tool reports `Selected option`. |
| Checkbox | `form_input(ref, true)` | ✅ Works (boolean). |
| Masked input (phone) | `form_input` / paste | ⚠️ Field's own JS mask **strips mismatched content** (alpha into a numeric field → empty). Not a tool bug — always read the field back after writing a masked field. |
| Password field (secret paste) | `clip-password` → focus field → `Ctrl+V` (`computer key`) | ✅ **Works** with proper focus. Verified with a **dummy** string only. See "Chosen ladders". |
| **Resume / CV file upload** | `file_upload(paths, ref)` | ❌ **BLOCKED.** "only files the user has shared with this session can be uploaded" — rejected for Downloads, scratchpad, and the repo cwd alike. |
| Resume upload — OS dialog plan-B | click file input → drive native "Open" via `mcp__computer-use__*` | ❌ **Not viable.** Chrome grants at tier `read` under computer-use (no clicks/typing); and clicking a file input through the extension (CDP) intercepts the chooser so no native dialog appears to drive. |
| Multi-tab / navigate / read_page / find | standard | ✅ Works (used throughout). |
| Radio buttons, custom JS listbox (React-select), date pickers | — | ⏸ **Not reached** — GATE already determined by upload. Backfill in a follow-up spike if the redesign clears the gate. |
| LinkedIn Easy Apply modal walk | — | ⏸ Not run (would touch the real LinkedIn session; deferred until upload is resolved). **Correction (user, 2026-07-05): Easy Apply must upload the per-job TAILORED resume PDF, not the generic one on the LinkedIn profile — so the upload blocker hits LinkedIn too, not just external ATS.** |
| Inbox verification loop | — | ⏸ Not run (deferred with the above). |

## Key mechanics learned (load-bearing for the runbook)

1. **`form_input` is the reliable primary fill ladder.** It sets values at the DOM level via the
   element `ref` and does **not** use screen coordinates, so it is immune to scroll/layout drift.
   Prefer it for every text/email/textarea/checkbox/native-select field.
2. **`computer` click/type/key are coordinate-based and go STALE.** A `ref`'s coordinates are
   captured at read time; after the page layout shifts (e.g. checking a box expands a section),
   clicking that `ref` lands on whatever is now at those pixels. Observed live: a click meant for a
   textarea focused a `<select>` instead. **Rule: re-`find`/re-`read_page` immediately before any
   coordinate click, or focus via JS.**
3. **Clipboard → `Ctrl+V` paste works, but only into a genuinely focused field.** The reliable
   focus is `javascript_tool` `element.focus()`; a stale-coordinate `computer` click is not. Once
   focused, `computer key "ctrl+v"` pastes the clipboard contents correctly (verified: dummy string
   landed in a textarea).
4. **`file_upload` is sandboxed to session-shared files** and there is no agent-reachable way to add
   a local file to that set from within a Claude Code run (attach-to-conversation is a user action).

## Chosen ladders (SP4 quotes these verbatim; backfills its `LADDER: PENDING SP1` markers)

**Field-fill ladder (all standard controls):**
1. `read_page`/`find` to get the field's current `ref`.
2. `form_input(ref, value)` — text, email, textarea, checkbox (`true`/`false`), native `<select>`
   (pass the visible option text).
3. Re-read the field (`read_page` or a targeted `javascript_tool` value check) to confirm it took;
   **for masked fields (phone, currency) always verify** — the mask may have silently stripped input.
4. For a custom JS listbox / React-select (not yet characterised): fall back to a fresh-coordinate
   `computer` click to open it, then click the option — re-`find` immediately before each click.

**Password-entry ladder (secret-safe — the ONLY permitted path):**
1. `python local/ats_accounts.py clip-password` (keyring → clipboard; prints only "copied").
2. Focus the password field with `javascript_tool`:
   `document.querySelector('input[type=password]').focus()` (never a stale-coordinate click).
3. `computer key "ctrl+v"` on that tab to paste.
4. Confirm it landed **without reading the value**:
   `javascript_tool` → `document.querySelector('input[type=password]').value.length` (a number > 0).
   Never read, echo, or `read_page` the value.
5. `python local/ats_accounts.py clip-clear` — **unconditionally**, including on any failure/abort.
6. If the field blocks paste (length stays 0 after Ctrl+V) → no secret-safe fallback exists → park
   `needs_human` "log in manually, then Re-queue", and still run `clip-clear`.

**Resume-upload ladder:** ❌ **none works today.** `file_upload` is blocked; the OS-dialog plan-B is
not drivable. Do not write an upload step until the redesign below is chosen.

## Redesign (required before the pilot)

The blocker is narrow: only *attaching the resume/cover-letter file* fails; the agent can navigate,
create accounts, fill every field, paste the password, and park at review. Options, best first:

- **A — Connect the resume folder to the Claude-in-Chrome extension (if supported).** The
  `file_upload` allowlist explicitly includes "folders the user has connected." If the extension
  exposes a connect-folder / file-access setting, the user connects `Generated_Resumes` once and
  `file_upload` works unattended thereafter. *Needs the user to confirm the extension has this and
  do it once.* Cleanest if available.
- **B — User attaches the batch's resume PDFs at kickoff.** The ~10 tailored PDFs are shared with
  the drain session when it launches. Works with today's tooling; manual per run, and the
  per-job subagents must receive the shared paths.
- **C — Agent fills everything, user attaches the resume + submits at review (recommended fallback).**
  The product *already* parks each job at its review page for the user to eyeball and submit. Fold
  the one "Attach resume" click into that existing manual step: the agent completes ~95% (account,
  all fields, cover-letter text, signature) and leaves the tab at review with a note
  "attach resume + submit". Degrades gracefully, needs no new capability, fits the current shape.
- ~~**D — LinkedIn-only unattended.**~~ **Invalid** — per the user, Easy Apply must upload the
  per-job tailored PDF, so LinkedIn is blocked by the same gap as external ATS. No flow is
  upload-free.

**Chosen (user, 2026-07-05): pursue A — connect the resume folder to the extension.** This is the
only path to true hands-off uploads across ALL flows (LinkedIn + external), since every application
needs the tailored PDF attached. Mechanism note: "folders the user has connected" is an
extension/host-side grant, *separate* from Claude Code's own filesystem access (a file inside the
Claude Code working directory was still rejected by `file_upload`) — so it must be configured on the
Claude-in-Chrome side by the user, then re-tested. **Fallback if A is unavailable: option C** (agent
fills ~95%, user attaches the tailored resume + submits at the review step) — now the universal
fallback for LinkedIn and external ATS alike. Re-run the deferred matrix rows (custom listbox,
LinkedIn walk, inbox loop) once uploads work.

## Side note (email decision, confirmed live)

W&M's own IT page (seen during the spike) states alumni accounts inactivate **16 months after the
graduating semester** — consistent with the user's "~1.5 years of access" for `jane.doe@example.com`.
Decision recorded: keep `wm.edu` for the pilot; revisit before it lapses.

GATE: FAIL — resume upload has no working path; redesign (option A/B/C above) required before SP5.

---

## UPDATE (2026-07-06): GATE CLEARED — Playwright `set_input_files` (option E)

The upload gate is **resolved**, by a route the SP1 spike didn't test: a **Playwright**-driven
browser instead of the Claude-in-Chrome extension. Proven end-to-end live on a real Greenhouse
application (Gotion, *Data Analyst*, 2026-07-06) — filled, **both PDFs attached**, submitted, and
a confirmation ("*your application has been received*") received.

### Why it works where `file_upload` couldn't

`page.set_input_files(selector, path)` sets the `<input type=file>` at the **browser-driver /
CDP layer** (`DOM.setFileInputFiles`), which is **below all three walls** the earlier routes hit:

1. the extension's **session-share policy** (why `mcp__claude-in-chrome__file_upload` rejected
   Downloads / scratchpad / cwd alike),
2. the page's **`Content-Security-Policy: connect-src`** (why a localhost-file-server + page-JS
   `fetch()` injection failed), and
3. computer-use's **read-tier** browser restriction (why the native OS file dialog wasn't drivable).

A plain absolute path to the tailored PDF just works. No folder-connect, no user attach step.

### The new option, ranked against A/B/C

- **E — Playwright driver (chosen for external boards).** True hands-off upload for accountless
  external ATS (Greenhouse/Lever-family) AND account-creation flows, with no per-run user setup.
  Trade-off: a **separate, fresh browser** (not the user's logged-in Chrome), so it does NOT carry
  the LinkedIn session — LinkedIn **Easy Apply** still wants the real-Chrome path. So: Playwright
  for external boards + account creation; Chrome-MCP retained for LinkedIn Easy Apply.
- A / C remain the fallbacks for the Chrome-MCP path if Playwright is unavailable.

### What shipped (graduated out of scratch, 2026-07-06)

- **`local/apply_verify.py`** — the emailed security-code gate handler (Greenhouse et al. email an
  N-char code after Submit). Extracts the code (mixed-case + numeric-OTP aware), and coordinates
  a file handshake (`code_request.json` / `code_response.json`) between the browser driver and the
  orchestrator's inbox reader (Outlook/Gmail MCP). Multi-box OTP inputs are filled char-by-char.
  Tests: `tests/test_apply_verify.py`.
- **`local/apply_playwright.py`** — the reusable Greenhouse-family driver: parse apply.md → fill the
  identity block → `set_input_files` both PDFs → **PARK at review (default; Submit never clicked)**.
  `--submit` is the explicitly-authorized end-to-end path (fill → Submit → `apply_verify` code gate
  → resubmit → confirm). It fills only fields it can SOURCE from apply.md; custom/EEO questions it
  can't source are left for the human at review (never guessed — safety invariant 2).
  Tests: `tests/test_apply_playwright.py`.

### Gotchas learned live (load-bearing for future runs)

- **Greenhouse security codes are mixed-case** (e.g. `tffCw7Xp`) — extraction must be `[A-Za-z0-9]`,
  not `[A-Z0-9]`; prefer a token containing a digit, fall back to all-letters.
- **The code field is a multi-box OTP** (`security-input-0`…`security-input-7`, one char per box) —
  fill by clicking the first box then typing char-by-char so it auto-advances; a single `.fill()`
  dumps the whole code into box 0.
- **Verify uploads by the filename chip, not the input** — Greenhouse swaps the `<input>` for a
  filename chip after attach, so re-reading the old input selector falsely reports "no file".

---

## UPDATE (2026-07-07): two follow-ups from the CITGO pilot

The 2026-07-06 CITGO SuccessFactors pilot surfaced two defects, now fixed.

### 1. `set_input_files` is not enough — some uploads need file-chooser interception

`page.set_input_files(selector, path)` only works when a reachable `<input type=file>` exists
in the frame you target. SuccessFactors' **"My Documents"** upload has NO such input in the main
frame — the widget is iframe-embedded / created on click, and the "+" opens a **native OS file
dialog**. `set_input_files` timed out ("waiting for locator input[type=file]") and the résumé
never attached.

**Fix — `apply_driver` `upload` action (file-chooser interception).** `with
page.expect_file_chooser(): click(trigger)` then `chooser.set_files(paths)` hooks the browser's
file chooser at the CDP layer, so it works **regardless of where (or whether) an `<input>`
exists** — iframe, shadow DOM, created-on-click, or a native dialog. This is the general robust
upload path; `set_files` (now also searching child iframes) remains for directly-addressable
inputs (e.g. Greenhouse `#resume`). Tests: `tests/test_apply_driver.py`.

### 2. A parked browser must outlive the agent — `launch` (detached) + `reopen`

A headed Playwright browser dies with the process that launched it. The runbook launched the
driver **inside the per-job subagent** (`serve … &`); when the subagent finished (parked,
reported back), that background process was reaped and the browser closed — so the parked window
was gone before the human returned, while the record still claimed it was "open" (blindly
asserted, never verified). That wasted the whole run's tokens.

**Fix:**
- **`apply_driver launch`** starts `serve` as a **detached** process (Windows: DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB, best-effort with a no-breakaway fallback;
  POSIX: `start_new_session`), output → `serve.log`, pid → `driver.pid`. The window now outlives
  the subagent AND the orchestrator. `apply_playwright --detach` does the same for the one-shot.
- **`apply_driver reopen --workdir DIR`** is the guarantee: if a window is ever gone anyway, it
  relaunches the SAME persistent profile (`DIR/profile`, logged-in session on disk) at
  `parked.json`'s URL — restoring draft-saving ATSes (SuccessFactors/Workday/iCIMS) exactly where
  the run left off. (A one-shot Greenhouse fill has no server draft → Re-queue instead.)
- Detachment is **best-effort** across OS/job-object policy; `reopen` + the persistent profile are
  what make "no wasted run" reliable. The skill now records the `reopen` command per parked job and
  never claims "still open" without it.

