"""Central settings layer: a single source of truth for user-editable options.

The dashboard (ui.py) and the watcher both read local/config.json. This module
describes WHICH keys are user-tunable (SETTINGS_SCHEMA) and provides safe
load/validate/save that:

  * fall back to a Field's default when a key is absent,
  * validate types and min/max ranges before writing,
  * MERGE into the existing backing file so keys not in the schema
    (resume_layout, backend, gemini_auth, ...) survive a save,
  * write atomically with a .bak backup so a crash mid-write can't corrupt
    config.json.

SP2 only backs onto the "config" target (local/config.json). The schema is a
flat list of Field rows grouped by `section` so the UI can render one labelled
input per row; SP3 will add Scraper / Scoring / Resume fields and new targets.
Every public function accepts an optional `targets` mapping so tests can point
the backing files at a tmp directory.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import envfile  # local module: comment-preserving .env reader/writer

HERE = Path(__file__).resolve().parent
# settings.py lives in local/, so the repo root (where scraper.py / score_jobs.py
# read their standalone JSON configs) is one level up.
ROOT = HERE.parent


@dataclass(frozen=True)
class Field:
    key: str            # config key (for env-target fields this is the ENV var name)
    label: str          # UI label
    type: str           # "int"|"float"|"str"|"bool"|"choice"|"multichoice"|"path"|"list"
    default: Any
    section: str        # "Dashboard"|"Scraper"|"Scoring"|"Resume"|"Apply"|"Credentials"|...
    target: str         # backing-file id (TARGET_FILES): config|search|scoring|apply|env
    help: str = ""
    choices: tuple = ()
    min: float | None = None
    max: float | None = None
    secret: bool = False        # mask in UI; never displayed/echoed; blank-on-save keeps existing
    path_kind: str = "dir"      # for type=="path": "dir" picks a folder, "file" picks a file
    optional: bool = False      # UI hint: blank is fine (no value needed to run)
    slider: bool = False        # UI hint: render a bounded int as a drag slider (needs min+max)


# Targets whose backing file is a .env (key=value), not JSON. Their Field.key is
# the literal environment-variable name, so values round-trip straight to .env.
ENV_TARGETS = {"env"}

# Gemini model ids offered in the model dropdowns (the recent 3.x family). These
# are EDITABLE dropdowns ("editable_choice"): pick one or type a custom id, so a
# new model id is never blocked — and a wrong pick can't silently break scoring.
GEMINI_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.1-pro-preview")


# Backing files, keyed by Field.target. The Scraper/Scoring sections write the
# root-level configs that scraper.py / score_jobs.py read standalone on the VM.
TARGET_FILES: dict[str, Path] = {
    "config": HERE / "config.json",
    "search": ROOT / "search_config.json",
    "scoring": ROOT / "scoring_config.json",
    "apply": ROOT / "apply_config.json",
    # Secrets, identity, and paths live in the git-ignored .env at the repo root,
    # the same file scraper.py / score_jobs.py / the tailor load at runtime.
    "env": ROOT / ".env",
}


SETTINGS_SCHEMA: list[Field] = [
    Field("min_score", "Min score to highlight", "int", 4, "Dashboard", "config",
          help="Jobs at/above this score are surfaced as high-priority.", min=1, max=5),
    Field("followup_days", "Follow-up after (days)", "int", 5, "Dashboard", "config",
          help="Days after applying before the tracker nudges a follow-up.", min=1, max=60,
          slider=True),
    Field("gdrive_root", "Job data folder", "path", "", "Dashboard", "config",
          help="Folder the dashboard reads scored CSVs from."),
    Field("mtime_stable_seconds", "Wait before opening a new file (seconds)", "int", 30,
          "Dashboard", "config",
          help="How long a freshly synced file must stop changing before the dashboard "
               "opens it — stops it reading a half-downloaded file. 30 is fine for most.",
          min=1, max=600),

    # --- Scraper: written to root-level search_config.json (read by scraper.py) ---
    Field("keywords", "Search keywords", "list",
          ['"Data Scientist"', '"AI Engineer"', '"AI Developer"', '"AI Scientist"',
           '"Software Engineer"', '"Software Developer"', '"Data Analyst"',
           '"Data Engineer"', '"LLM"', '"Analytics Engineer"', '"Decision Scientist"',
           '"Generative AI"', '"Gen AI"', '"GenAI"', '"Quant"',
           '"Implementation Engineer"', '"Agentic"', '"Applied AI"',
           '"Artificial Intelligence"', '"Business Analyst"'],
          "Scraper", "search",
          help="One search phrase per line. Wrap any phrase of two or more words in "
               '"double quotes" (e.g. "Data Scientist") so it is searched as a phrase; '
               "single words need no quotes. Each line runs once per ticked remote type."),
    Field("remote_types", "Remote types", "multichoice", ["Hybrid", "On-site"],
          "Scraper", "search",
          help="Which workplace types to search. Each ticked type runs once per keyword.",
          choices=("On-site", "Remote", "Hybrid")),
    Field("limit_per_input", "Postings per search", "int", 100, "Scraper", "search",
          help="Max postings collected per (keyword x remote type). Higher = more spend.",
          min=1, max=500),
    Field("exclude_window_days", "Re-scrape exclusion (days)", "int", 14, "Scraper", "search",
          help="Skip postings scraped within this many days (avoids re-billing live jobs).",
          min=0, max=90, slider=True),
    Field("location", "Location", "str", "United States", "Scraper", "search",
          help="Geographic location filter for searches."),
    Field("country", "Country code", "str", "US", "Scraper", "search",
          help="Two-letter country code (e.g. US, GB, CA)."),
    Field("time_range", "Time range", "choice", "Past 24 hours", "Scraper", "search",
          help="Only collect postings newer than this.",
          choices=("Past 24 hours", "Past week", "Past month", "Any time")),
    Field("job_type", "Job type", "choice", "Full-time", "Scraper", "search",
          help="Employment type to search for.",
          choices=("Full-time", "Part-time", "Contract", "Temporary",
                   "Internship", "Volunteer", "Other")),
    Field("experience_level", "Experience level", "choice", "Entry level", "Scraper", "search",
          help="Seniority filter.",
          choices=("Internship", "Entry level", "Associate",
                   "Mid-Senior level", "Director", "Executive")),

    # --- Scoring: written to root-level scoring_config.json (read by score_jobs.py) ---
    Field("stage1_model", "Stage-1 model", "editable_choice", "gemini-3.1-flash-lite",
          "Scoring", "scoring", choices=GEMINI_MODELS,
          help="Advanced: cheap model that scores every surviving job 1-5. Pick from the "
               "list or type a model ID your account can use — a wrong name silently "
               "breaks scoring."),
    Field("stage2_model", "Stage-2 model", "editable_choice", "gemini-3.5-flash",
          "Scoring", "scoring", choices=GEMINI_MODELS,
          help="Advanced: deeper model for jobs that pass the Stage-2 threshold. Pick from "
               "the list or type a model ID your account can use — a wrong name silently "
               "breaks scoring."),
    Field("stage1_concurrency", "Stage-1 concurrency", "int", 6, "Scoring", "scoring",
          help="Parallel Stage-1 LLM calls.", min=1, max=50, slider=True),
    Field("stage2_concurrency", "Stage-2 concurrency", "int", 4, "Scoring", "scoring",
          help="Parallel Stage-2 LLM calls.", min=1, max=50, slider=True),
    Field("stage2_threshold", "Stage-2 threshold", "int", 4, "Scoring", "scoring",
          help="Stage-1 score at/above which a job gets deep Stage-2 analysis.", min=1, max=5,
          slider=True),
    Field("max_scored_per_run", "Max scored per run", "int", 800, "Scoring", "scoring",
          help="Spend guard: cap on LLM-scored jobs per run.", min=1, max=5000),
    Field("rescore_cap", "Rescore cap", "int", 200, "Scoring", "scoring",
          help="Spend guard: cap on failed/missing master rows retried per run.", min=0, max=5000),
    Field("min_filter_years", "Min required years cutoff", "int", 1, "Scoring", "scoring",
          help="Roles requiring at least this many years of experience are filtered out.",
          min=0, max=20, slider=True),

    # --- Resume: artifact toggles + cover-letter tone (local/config.json) ---
    # Line budgets / required sections stay where they already are: the
    # "Resume layout…" button (project line targets) and the master_experience
    # yaml `tailor:` block. These four are artifact toggles + tone only.
    Field("tailor_cover_letter", "Generate cover letter", "bool", False, "Resume", "config",
          help="When tailoring, also generate a cover letter PDF."),
    Field("tailor_ats_report", "Write ATS report", "bool", True, "Resume", "config",
          help="Write ats_report.txt (keyword coverage) for each tailored résumé."),
    Field("tailor_prep_sheet", "Generate interview-prep sheet", "bool", False, "Resume", "config",
          help="Also generate the interview-prep sheet during tailoring (otherwise "
               "it's on-demand via the Interview prep button)."),
    Field("resume_tone", "Cover-letter tone", "choice", "professional", "Resume", "config",
          help="Tone used when generating the cover letter.",
          choices=("professional", "concise", "enthusiastic", "impactful")),

    # Apply-form answers (work auth, sponsorship, EEO, "how did you hear") are NOT
    # configured here. They live in the richer Apply Answers tab (per-question,
    # fixed/open-ended, needs-review), which writes apply_answers.json — the single
    # source of truth the apply pipeline reads. apply_config.DEFAULTS only seeds
    # that store on first run.

    # --- Credentials: API keys / tokens, written to the git-ignored .env -------
    # secret=True fields are masked and write-only: the stored value is never
    # shown; saving with the box blank keeps the existing value. Field.key is the
    # exact environment-variable name the pipeline reads.
    Field("BRIGHT_DATA_API_TOKEN", "Bright Data API token", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Needed to run the scraper. Bright Data dashboard - Account settings - API tokens."),
    Field("BRIGHT_DATA_DATASET_ID", "Bright Data dataset ID", "str", "",
          "Credentials", "env", optional=True,
          help="The id of your LinkedIn-jobs dataset in Bright Data (not secret - an identifier)."),
    Field("GEMINI_API_KEYS", "Gemini API key pool", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Comma-separated Gemini API keys for the scorer (no spaces). Get keys at "
               "aistudio.google.com. Leave blank to use your Google Cloud project instead."),
    Field("RESUME_TAILOR_GEMINI_API_KEY", "Gemini API key (resume tailor)", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Single Gemini API key for the resume tailor when the engine is set to "
               "'api_key'. Only needed if you don't have a Google Cloud project."),

    # --- Connection & paths: non-secret identity / locations, also in .env -----
    Field("GOOGLE_CLOUD_PROJECT", "Google Cloud project ID", "str", "",
          "Connection & paths", "env", optional=True,
          help="Project with Vertex AI enabled (for Gemini scoring + tailoring). Leave blank "
               "if you score with the Gemini API key pool above."),
    Field("GOOGLE_CLOUD_LOCATION", "Google Cloud location", "choice", "global",
          "Connection & paths", "env",
          help="Vertex AI region. 'global' works for most users.",
          choices=("global", "us-central1", "us-east1", "us-west1", "europe-west1")),
    Field("RESUME_TAILOR_CANDIDATE", "Your name (resume filenames)", "str", "Your_Name",
          "Connection & paths", "env",
          help="Used in generated resume filenames. Use underscores instead of spaces."),
    Field("RESUME_TAILOR_OUTPUT", "Resume output folder", "path", "",
          "Connection & paths", "env", path_kind="dir", optional=True,
          help="Where tailored resumes are saved. Blank = your Downloads/Generated_Resumes."),
    Field("PDFLATEX_PATH", "pdflatex path", "path", "pdflatex",
          "Connection & paths", "env", path_kind="file", optional=True,
          help="Path to pdflatex (MiKTeX/TeX Live). Leave as 'pdflatex' if it's on your PATH."),
    Field("LINKEDIN_CHROME_ACCOUNT", "Chrome profile (Google email)", "str", "",
          "Connection & paths", "env", optional=True,
          help="Open job links in the Chrome profile signed in to this Google account. "
               "Blank = your default browser."),

    # --- Engine: which Gemini backend the resume tailor bills (local/config.json) -
    Field("gemini_auth", "Resume tailor engine", "choice", "vertex",
          "Engine", "config",
          help="'vertex' bills your Google Cloud project (above). 'api_key' uses the single "
               "Gemini API key (above) - pick this if you don't have a Cloud project.",
          choices=("vertex", "api_key")),

    # --- Resume tailor models: which Gemini model each tailoring stage uses, ----
    # written to .env (read by local/resume_tailor/config.py as RESUME_TAILOR_MODEL_*).
    # Editable dropdowns: pick a 3.x model or type a custom id.
    Field("RESUME_TAILOR_MODEL_FLASH_LITE", "Tailor model — fast (selection)",
          "editable_choice", "gemini-3.1-flash-lite", "Engine", "env", choices=GEMINI_MODELS,
          help="Cheapest model — the bullet-selection / quick stages of tailoring."),
    Field("RESUME_TAILOR_MODEL_FLASH", "Tailor model — standard (writing)",
          "editable_choice", "gemini-3.5-flash", "Engine", "env", choices=GEMINI_MODELS,
          help="Default model — re-phrasing bullets and the cover letter."),
    Field("RESUME_TAILOR_MODEL_PRO", "Tailor model — deep (pro)",
          "editable_choice", "gemini-3.5-flash", "Engine", "env", choices=GEMINI_MODELS,
          help="Highest-quality tier — set to gemini-3.1-pro-preview for the strongest "
               "writing (slower / pricier)."),
]


def _resolve_targets(targets: dict[str, Path] | None) -> dict[str, Path]:
    return TARGET_FILES if targets is None else targets


def _read_file(path: Path) -> dict[str, Any]:
    """Parse a backing JSON file, or {} when missing/unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_target(target_id: str, path: Path | None) -> dict[str, Any]:
    """Read a backing store as a {key: value} dict, picking the right parser for
    its target (env files vs JSON). {} when the path is unset/missing."""
    if path is None:
        return {}
    if target_id in ENV_TARGETS:
        return envfile.read(path)
    return _read_file(path)


def load(targets: dict[str, Path] | None = None) -> dict[str, Any]:
    """Return {key: stored-value-or-default} for every schema Field.

    Reads each backing file once and looks each Field up in its own target,
    so the result is the effective configuration the UI should display.
    """
    targets = _resolve_targets(targets)
    cache: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for f in SETTINGS_SCHEMA:
        if f.target not in cache:
            cache[f.target] = _read_target(f.target, targets.get(f.target))
        store = cache[f.target]
        values[f.key] = store[f.key] if f.key in store else f.default
    return values


def secret_status(targets: dict[str, Path] | None = None) -> dict[str, bool]:
    """{key: is-it-set} for every secret Field, WITHOUT returning the value.

    The config GUI uses this to show "configured / not set" next to a masked,
    write-only secret box — so a stored token is never loaded into a widget.
    """
    targets = _resolve_targets(targets)
    cache: dict[str, dict[str, Any]] = {}
    out: dict[str, bool] = {}
    for f in SETTINGS_SCHEMA:
        if not f.secret:
            continue
        if f.target not in cache:
            cache[f.target] = _read_target(f.target, targets.get(f.target))
        out[f.key] = bool(str(cache[f.target].get(f.key, "")).strip())
    return out


def _coerce_ok(f: Field, value: Any) -> bool:
    """True when `value` is the right Python type for Field `f`."""
    if f.type == "int":
        # bool is a subclass of int; reject it for int fields.
        return isinstance(value, int) and not isinstance(value, bool)
    if f.type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if f.type == "bool":
        return isinstance(value, bool)
    if f.type in ("str", "path"):
        return isinstance(value, str)
    if f.type == "choice":
        return value in f.choices
    if f.type == "editable_choice":
        # editable: any string is allowed (pick from choices OR type a custom id).
        return isinstance(value, str)
    if f.type in ("list", "multichoice"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return True


def validate(values: dict[str, Any]) -> dict[str, str]:
    """Return {key: error_message} for invalid values; empty dict means valid.

    Only keys present in `values` AND in the schema are checked.
    """
    errors: dict[str, str] = {}
    by_key = {f.key: f for f in SETTINGS_SCHEMA}
    for key, value in values.items():
        f = by_key.get(key)
        if f is None:
            continue
        if not _coerce_ok(f, value):
            errors[key] = f"Expected {f.type}, got {type(value).__name__}."
            continue
        if f.type in ("int", "float"):
            if f.min is not None and value < f.min:
                errors[key] = f"Must be >= {f.min}."
            elif f.max is not None and value > f.max:
                errors[key] = f"Must be <= {f.max}."
        elif f.type == "multichoice":
            bad = [v for v in value if v not in f.choices]
            if bad:
                errors[key] = f"Not allowed: {', '.join(bad)}."
    return errors


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write `data` as JSON to `path`, backing up any existing file to .bak.

    Copy existing -> path.bak, write to a same-dir PID-tagged temp file, then
    os.replace onto the real path (atomic on the same filesystem).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save(values: dict[str, Any], targets: dict[str, Path] | None = None) -> None:
    """Validate then persist `values`, grouped by Field.target.

    Raises ValueError(errors) if validation fails. For each backing file, merge
    the schema-owned values into the file's existing contents so unrelated keys
    survive, then write atomically with a .bak backup.
    """
    errors = validate(values)
    if errors:
        raise ValueError(errors)

    targets = _resolve_targets(targets)
    by_key = {f.key: f for f in SETTINGS_SCHEMA}

    # key -> values to write, grouped by target id.
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        f = by_key.get(key)
        if f is None:
            continue
        grouped.setdefault(f.target, {})[key] = value

    for target_id, updates in grouped.items():
        path = targets.get(target_id)
        if path is None:
            continue
        if target_id in ENV_TARGETS:
            # envfile.update merges in place (keeps comments + unknown keys) and
            # backs up to .bak itself, so no read-merge-write dance here.
            envfile.update(Path(path), {k: str(v) for k, v in updates.items()})
        else:
            merged = _read_file(path)
            merged.update(updates)
            _atomic_write(Path(path), merged)
