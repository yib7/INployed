# Credits

This project stands on a lot of other people's work.

## Résumé template
- The LaTeX résumé layout in `resume_tailor_files/resume_template.tex` is derived
  from the widely-used **"Jake's Resume"** template by Jake Gutierrez
  (https://github.com/jakegut/resume), MIT-licensed. The `\resumeItem`,
  `\resumeSubheading`, and section macros come from that template; the generation
  pipeline fills them from `master_experience.yaml`.

## Avoid-AI-writing rules
- The optional cover-letter style pass in `local/resume_tailor/aiwriting.py`
  (Settings → Resume → "Strip AI writing patterns from the cover letter", off by
  default) vendors a bounded extract of the **avoid-ai-writing** skill, version
  3.18.0, by **Conor Bronsdon**, MIT-licensed, read from the local Claude skill
  at `~/.claude/skills/avoid-ai-writing/SKILL.md`. The prompt rules and the
  deterministic ban list are a letter-relevant subset of that skill's pattern
  catalogue, re-shaped to match this pipeline's existing style gate; the skill
  itself is not bundled. Its tiered vocabulary table, which the ban list draws
  from, is in turn credited upstream to the vocabulary research in
  https://github.com/brandonwise/humanizer.

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
