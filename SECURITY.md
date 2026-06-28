# Security Policy

INployed is a personal, single-developer project. There is no bug-bounty program, but
genuine vulnerability reports are welcome and taken seriously.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and
click **Report a vulnerability**. That keeps the details private until a fix is out. Please
do not file a public issue for a security problem.

Include enough to reproduce it: affected file or component, the version or commit, and the
impact you observed. Expect an initial reply within about a week.

## Scope

This is a desktop + local-CLI tool with an optional self-hosted GCP scraper VM. The reports
most relevant to it:

- Secrets handling. Keys live only in a git-ignored `.env` (and, by the user's choice, in the
  local `settings_archive/` snapshots, also git-ignored). A path that logs, prints, transmits,
  or commits a secret is in scope.
- The résumé/apply pipeline reading untrusted input (a pasted job description or URL, a scraped
  posting) in a way that escapes its sandbox, runs unintended code, or writes outside the
  intended output folder. LaTeX is compiled with `-no-shell-escape`; a bypass is in scope.
- The dashboard or VM controls performing an action the user did not confirm.

Out of scope: anything requiring a key the user themselves supplied to misbehave against its
own intended service (Vertex AI, Bright Data), and the security of those third-party services.

## Supported versions

Only the latest release on `main` is supported. Fixes land there; older tags are not patched.
