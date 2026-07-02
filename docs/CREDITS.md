# Credits

This project stands on a lot of other people's work.

## Résumé template
- The LaTeX résumé layout in `resume_tailor_files/resume_template.tex` is derived
  from the widely-used **"Jake's Resume"** template by Jake Gutierrez
  (https://github.com/jakegut/resume), MIT-licensed. The `\resumeItem`,
  `\resumeSubheading`, and section macros come from that template; the generation
  pipeline fills them from `master_experience.yaml`.

## Word lists
- `resume_tailor_files/active_words.md` (the composer's verb palette) was compiled
  from a third-party "action verbs" reference handout, reorganized by skill category
  for this pipeline.

## Services & APIs
- **Google Gemini** via **Vertex AI**: job relevance scoring and résumé composition.
- **Bright Data**: LinkedIn job dataset collection.
- **Google Drive** + **rclone**: syncing scraped results from the VM to the desktop.
- **MiKTeX** (`pdflatex`): LaTeX to PDF compilation.

## Python libraries
pandas · google-genai · aiohttp · PyYAML · pypdf · markdownify · python-dotenv ·
PySide6 (Qt) · pytest · pytest-qt · and the Python standard library (asyncio, sqlite3, argparse).

If you reuse this project, please keep this file and the upstream template
attribution.
