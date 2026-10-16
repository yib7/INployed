# Vendored: avoid-ai-writing detector

Unmodified copies from Conor Bronsdon's
[avoid-ai-writing](https://github.com/conorbronsdon/avoid-ai-writing), MIT
License (see `LICENSE` and `NOTICE.md`).

- Version: 3.38.0 (npm `avoid-ai-writing-detector@3.38.0`)
- Commit: `0a4f1ad101683fd8734241ed46430a0b4a9af729`

| File | Upstream path |
|------|---------------|
| `detector/patterns.js` | `detector/patterns.js` (the engine; no dependencies) |
| `bin/avoid-ai-writing.js` | `bin/avoid-ai-writing.js` (CLI: text on stdin, JSON on stdout) |

`resume_tailor/aidetector.py` runs the CLI with Node when the Settings toggle
"Run the upstream AI-writing detector on the cover letter" is on. Node is
optional: without it the check is skipped.

To update, copy the same two files from a newer upstream release, then bump
`UPSTREAM_VERSION` in `aidetector.py` and the version and commit above.
