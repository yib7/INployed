"""Central settings layer: a single source of truth for user-editable options.

The dashboard and the watcher both read local/config.json. This module
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
    secret: bool = False        # a credential: shown from the local .env, writes the box as-is
    path_kind: str = "dir"      # for type=="path": "dir" picks a folder, "file" picks a file
    optional: bool = False      # UI hint: blank is fine (no value needed to run)
    slider: bool = False        # UI hint: render a bounded int as a drag slider (needs min+max)
    warn_above: float | None = None  # slider only: show warn_text live when value exceeds this
    warn_text: str = ""              # the caution shown under the slider past warn_above


# Targets whose backing file is a .env (key=value), not JSON. Their Field.key is
# the literal environment-variable name, so values round-trip straight to .env.
ENV_TARGETS = {"env"}

# Gemini model ids offered in the model dropdowns (the recent 3.x family). These
# are EDITABLE dropdowns ("editable_choice"): pick one or type a custom id, so a
# new model id is never blocked — and a wrong pick can't silently break scoring.
GEMINI_MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.1-pro-preview")

# Claude model ids offered in the Claude model dropdowns (also editable_choice).
CLAUDE_MODELS = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8")


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
    Field("tailor_open_folder", "Open output folder after tailoring", "bool", False,
          "Dashboard", "config",
          help="When on, the tailored résumé's folder opens in File Explorer after each run. "
               "Off (default) keeps the screen tidy when you tailor several jobs at once — reach "
               "the folder from the Apply panel's 'Open folder' button or the path in the status bar."),
    Field("stale_after_hours", "Flag data as stale after (hours)", "int", 36,
          "Dashboard", "config",
          help="The Stats tab warns that the pipeline may have failed when the newest run is "
               "older than this. Discovery runs a few times a day, so 36h means a missed "
               "day stands out.", min=6, max=336),

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
    Field("provider", "Scoring provider", "choice", "gemini", "Scoring", "scoring",
          help="Which AI service scores jobs when scoring runs ON THIS PC. 'claude' uses "
               "your local Claude Code CLI (subscription). The cloud VM always scores "
               "with Gemini regardless of this setting. Applies from the next scoring run.",
          choices=("gemini", "claude")),
    Field("stage1_model_claude", "Stage-1 model (Claude)", "editable_choice",
          "claude-haiku-4-5", "Scoring", "scoring", choices=CLAUDE_MODELS,
          help="Used only when Scoring provider is 'claude'."),
    Field("stage2_model_claude", "Stage-2 model (Claude)", "editable_choice",
          "claude-sonnet-5", "Scoring", "scoring", choices=CLAUDE_MODELS,
          help="Used only when Scoring provider is 'claude'."),
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

    # --- Resume: artifact toggles + cover-letter tone (config.json) ---
    # Layout controls live in the Resume Data tab's "Resume Layout (bullet sizing)"
    # section: per-bullet line budgets AND the project count + at-most/exactly-N
    # mode (jobsdata.save_projects_count). Required sections come from the
    # master_experience yaml `tailor:` block. Here: artifact toggles and tone only.
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

    # --- Auto-apply: the batch queue knobs (config.json). Read by the dashboard's
    # _queue_for_auto_apply and by apply_queue.build_context() for the agent run. ---
    Field("auto_apply_batch_cap", "Max jobs queued per batch", "int", 10,
          "Auto-apply", "config", min=1, max=25,
          help="At most this many jobs go into the auto-apply queue per 'Queue for "
               "auto-apply' action. ~10 keeps a batch reviewable in one sitting (every "
               "application is parked at its review page for you — never submitted)."),
    Field("auto_apply_inbox_url", "Fallback signup inbox URL", "str", "https://mail.google.com",
          "Auto-apply", "config",
          help="Fallback webmail inbox the auto-apply agent opens for verification "
               "emails when your signup email's domain isn't in 'Inbox by email domain' "
               "below. Must be signed in already in Chrome."),
    Field("auto_apply_inbox_map", "Inbox by email domain", "list",
          ["gmail.com https://mail.google.com",
           "googlemail.com https://mail.google.com",
           "outlook.com https://outlook.live.com/mail/",
           "hotmail.com https://outlook.live.com/mail/",
           "live.com https://outlook.live.com/mail/",
           "msn.com https://outlook.live.com/mail/",
           "wm.edu https://outlook.office.com/mail/"],
          "Auto-apply", "config",
          help="One 'emaildomain webmail-url' per line, so the agent opens the RIGHT "
               "inbox for the email it signs up with (e.g. an @wm.edu address is "
               "Microsoft 365 / Outlook, not Gmail). The domain of your signup email "
               "(basics.email) is looked up here first; unlisted domains use the "
               "fallback inbox above. Whichever inbox is used must be signed in in Chrome. "
               "Keep the provider defaults in sync with apply_queue.DEFAULT_INBOX_MAP."),

    # --- Settings history: snapshot every Save so settings can be rolled back ---
    # All four live in local/config.json. Snapshots copy every settings file
    # (including the secret-bearing .env) into a git-ignored settings_archive/.
    Field("archive_enabled", "Snapshot settings on every Save", "bool", True,
          "Settings history", "config",
          help="When on, each Save copies all your settings into a dated folder so you can "
               "roll back later via 'Restore from archive...'. Turn off to stop snapshotting."),
    Field("archive_prune_mode", "Old-snapshot cleanup", "choice", "Keep everything",
          "Settings history", "config",
          choices=("Keep everything", "Keep newest N", "Delete older than N days"),
          help="How to stop snapshots piling up. 'Keep everything' never deletes (the default)."),
    Field("archive_prune_keep", "Snapshots to keep (newest N)", "int", 20,
          "Settings history", "config", min=1, max=1000,
          help="With 'Keep newest N': how many of the most recent snapshots to keep."),
    Field("archive_prune_days", "Delete snapshots older than (days)", "int", 30,
          "Settings history", "config", min=1, max=3650,
          help="With 'Delete older than N days': snapshots older than this are removed on Save."),

    # Apply-form answers (work auth, sponsorship, EEO, "how did you hear") are NOT
    # configured here. They live in the richer Apply Answers tab (per-question,
    # fixed/open-ended, needs-review), which writes apply_answers.json — the single
    # source of truth the apply pipeline reads. apply_config.DEFAULTS only seeds
    # that store on first run.

    # --- Credentials: API keys / tokens, written to the git-ignored .env -------
    # secret=True fields show their saved value in the GUI (read straight from the
    # local .env) and write whatever the box holds — clearing it removes the key.
    # Field.key is the exact environment-variable name the pipeline reads.
    Field("BRIGHT_DATA_API_TOKEN", "Job-data API token", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Needed for job discovery. Create one in your job-data API dashboard - API tokens."),
    Field("BRIGHT_DATA_DATASET_ID", "Job-data dataset ID", "str", "",
          "Credentials", "env", optional=True,
          help="The job-postings dataset to query - an identifier, not a secret."),
    Field("GEMINI_API_KEYS", "Gemini API keys (job scorer)", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Powers the JOB SCORER, which rates every collected job. A pool of one or more keys, "
               "comma-separated with no spaces, that it rotates through to spread rate limits. This "
               "is SEPARATE from the resume-tailor key below. Get keys at aistudio.google.com; leave "
               "blank to score with your Google Cloud project instead."),
    Field("RESUME_TAILOR_GEMINI_API_KEY", "Gemini API key (resume tailor)", "str", "",
          "Credentials", "env", secret=True, optional=True,
          help="Powers the RESUME TAILOR only, and only when its engine (below) is set to 'api_key'. "
               "A SINGLE key, kept separate from the scorer's pool above so the two can use different "
               "accounts or quotas. Leave blank if the tailor uses your Google Cloud project "
               "(engine 'vertex')."),

    # --- Connection & paths: non-secret identity / locations, also in .env -----
    Field("GOOGLE_CLOUD_PROJECT", "Google Cloud project ID", "str", "",
          "Connection & paths", "env", optional=True,
          help="Project with Vertex AI enabled (for Gemini scoring + tailoring). Leave blank "
               "if you use the Gemini API keys (job scorer) above instead."),
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

    Field("tailor_provider", "Resume tailor provider", "choice", "gemini",
          "Engine", "config",
          help="Which AI service tailors resumes. 'gemini' uses the Google engine above. "
               "'claude' runs your locally installed Claude Code CLI on your claude.ai "
               "subscription (run `claude` once to log in). Takes effect on the next "
               "tailor run -- no restart.",
          choices=("gemini", "claude")),

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
    Field("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "Claude model — fast (selection)",
          "editable_choice", "claude-haiku-4-5", "Engine", "env", choices=CLAUDE_MODELS,
          help="Claude provider only: cheapest tier (bullet selection / quick stages). "
               "Restart the dashboard after changing (.env is read at startup)."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_FLASH", "Claude model — standard (writing)",
          "editable_choice", "claude-sonnet-5", "Engine", "env", choices=CLAUDE_MODELS,
          help="Claude provider only: re-phrasing bullets and the cover letter. "
               "Restart the dashboard after changing (.env is read at startup)."),
    Field("RESUME_TAILOR_CLAUDE_MODEL_PRO", "Claude model — deep (pro)",
          "editable_choice", "claude-opus-4-8", "Engine", "env", choices=CLAUDE_MODELS,
          help="Claude provider only: highest-quality tier (rephrase / cover letter). "
               "Restart the dashboard after changing (.env is read at startup)."),

    # --- VM (cloud scraper): NON-secret gcloud connection identifiers, in .env --
    # The VM tab pushes config/schedule/pause via `gcloud compute`. Auth is your
    # existing `gcloud auth login` — no SSH password or key is ever stored.
    # vm_enabled is the section master switch (local, non-secret bool in config.json):
    # off (the default) hides the whole VM area in the GUI and silences push prompts.
    Field("vm_enabled", "Enable VM features", "bool", False, "VM (cloud scraper)", "config",
          help="Turn on to manage a cloud job-discovery VM from here (schedule, pause, push config). "
               "Off hides all VM settings and never prompts to push — leave off if you don't use a VM."),
    Field("VM_INSTANCE", "VM instance name", "str", "", "VM (cloud scraper)", "env",
          optional=True,
          help="GCP instance that runs job discovery (e.g. scraper-vm). Blank = VM actions disabled."),
    Field("VM_ZONE", "VM zone", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="Compute zone, e.g. us-east1-c."),
    Field("VM_PROJECT", "GCP project", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="GCP project id the instance lives in (optional if gcloud has a default)."),
    Field("VM_USER", "VM Linux user", "str", "", "VM (cloud scraper)", "env", optional=True,
          help="Linux account on the VM that owns the discovery run (run_scraper.sh, crontab, data)."),
    Field("VM_REMOTE_DIR", "VM home dir", "str", "~", "VM (cloud scraper)", "env",
          help="Remote dir the discovery files live in. Usually ~ (the Linux user's home)."),
    Field("VM_GCLOUD_PATH", "gcloud path", "path", "gcloud", "VM (cloud scraper)", "env",
          path_kind="file", optional=True,
          help="Path to the gcloud CLI. Leave as 'gcloud' if it's on your PATH."),
    # --- Local watcher task: keep the LinkedInJobsWatcher scheduled task in step --
    # with the VM schedule (local, non-secret; the VM tab's buttons use these too).
    Field("local_task_autosync", "Auto-sync local watcher task", "bool", False,
          "VM (cloud scraper)", "config",
          help="When on, applying a schedule to the VM also re-registers the local "
               "LinkedInJobsWatcher task so it checks for fresh results after each run. "
               "Syncs off the VM's wall-clock run times, so it assumes the VM shares "
               "your timezone. Off = the local task's triggers never move."),
    Field("local_task_offsets", "Watcher check offsets (minutes)", "str", "30,50,70",
          "VM (cloud scraper)", "config",
          help="Minutes after each VM run time the local watcher checks for fresh "
               "results, comma-separated (e.g. 30,50,70 = three checks per run)."),
]


# Friendly filename shown next to each field in the config GUI so a user can find
# and inspect the file a value is saved to themselves (keyed by Field.target).
STORAGE_LABELS: dict[str, str] = {
    "config": "config.json",
    "search": "search_config.json",
    "scoring": "scoring_config.json",
    "apply": "apply_config.json",
    "env": ".env",
}


def storage_location(field: Field) -> str:
    """The friendly filename a Field's value is saved to (for the GUI 'stored in'
    tag). Falls back to the raw target id for any unmapped target."""
    return STORAGE_LABELS.get(field.target, field.target)


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
