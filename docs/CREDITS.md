# Credits

This project stands on a lot of other people's work.

## Résumé template
- The LaTeX résumé layout in `resume_tailor_files/resume_template.tex` is derived
  from the widely-used **"Jake's Resume"** template by Jake Gutierrez
  (https://github.com/jakegut/resume), MIT-licensed. The `\resumeItem`,
  `\resumeSubheading`, and section macros come from that template; the generation
  pipeline fills them from `master_experience.yaml`. The MIT permission notice
  travels with the file itself, in the comment header at the top of
  `resume_template.tex`.

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
pandas · google-genai · aiohttp · PyYAML · ruamel.yaml · pypdf · markdownify ·
python-dotenv · tzdata · requests · Send2Trash · keyring · PySide6 (Qt) · pytest ·
pytest-qt · pytest-timeout · ruff · and the Python standard library (asyncio, sqlite3,
argparse). The VM pin set (`scripts/requirements-vm.txt`) adds **numpy**, whose own
declared expression is `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`.

Four more are optional and installed only if you want the feature they serve, so
`requirements.txt` lists them without installing them: **playwright** (Apache-2.0), the
advanced auto-apply driver; and, for maintainers regenerating art and README media,
**Pillow** (MIT-CMU) plus **imageio-ffmpeg** (BSD-2), which downloads its own **FFmpeg**
binary (LGPL-2.1-or-later) at first use. No FFmpeg binary, and nothing else from that
list, is committed here.

None of these are redistributed with this project; `pip` installs each from PyPI under
its own license. Across the full pinned dependency tree the licenses are MIT, BSD-2/3,
0BSD, Apache-2.0, PSF, Zlib, CC0-1.0 and MPL-2.0 (certifi) — the Zlib, CC0-1.0 and 0BSD
arms coming from numpy's composite `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`,
pulled in under pandas. All of them are permissive, and the MIT release license permits
every one.
The one copyleft dependency is **PySide6** (with PySide6-Essentials, PySide6-Addons and
shiboken6), licensed LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only, or commercially from
The Qt Company. See the License section of the README for why a source-only distribution
satisfies the LGPL here.

If you reuse this project, please keep this file and the upstream template
attribution.
