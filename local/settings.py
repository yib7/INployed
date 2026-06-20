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

HERE = Path(__file__).resolve().parent
# settings.py lives in local/, so the repo root (where scraper.py / score_jobs.py
# read their standalone JSON configs) is one level up.
ROOT = HERE.parent


@dataclass(frozen=True)
class Field:
    key: str            # config key
    label: str          # UI label
    type: str           # "int" | "float" | "str" | "bool" | "choice" | "path" | "list"
    default: Any
    section: str        # "Dashboard" | "Scraper" | "Scoring" | "Resume"
    target: str         # backing-file id; SP2 only uses "config"
    help: str = ""
    choices: tuple = ()
    min: float | None = None
    max: float | None = None


# Backing files, keyed by Field.target. The Scraper/Scoring sections write the
# root-level configs that scraper.py / score_jobs.py read standalone on the VM.
TARGET_FILES: dict[str, Path] = {
    "config": HERE / "config.json",
    "search": ROOT / "search_config.json",
    "scoring": ROOT / "scoring_config.json",
}


SETTINGS_SCHEMA: list[Field] = [
    Field("min_score", "Min score to highlight", "int", 4, "Dashboard", "config",
          help="Jobs at/above this score are surfaced as high-priority.", min=1, max=5),
    Field("followup_days", "Follow-up after (days)", "int", 5, "Dashboard", "config",
          help="Days after applying before the tracker nudges a follow-up.", min=1, max=60),
    Field("gdrive_root", "Job data folder", "path", "", "Dashboard", "config",
          help="Folder the dashboard reads scored CSVs from."),
    Field("mtime_stable_seconds", "File settle (seconds)", "int", 30, "Dashboard", "config",
          help="How long a file must be unchanged before the watcher reads it.", min=1, max=600),

    # --- Scraper: written to root-level search_config.json (read by scraper.py) ---
    Field("keywords", "Search keywords", "list",
          ['"Data Scientist"', '"AI Engineer"', '"AI Developer"', '"AI Scientist"',
           '"Software Engineer"', '"Software Developer"', '"Data Analyst"',
           '"Data Engineer"', '"LLM"', '"Analytics Engineer"', '"Decision Scientist"',
           '"Generative AI"', '"Gen AI"', '"GenAI"', '"Quant"',
           '"Implementation Engineer"', '"Agentic"', '"Applied AI"',
           '"Artificial Intelligence"', '"Business Analyst"'],
          "Scraper", "search",
          help="One LinkedIn search phrase per line (quote multi-word phrases). "
               "Each fans out to one search per remote type."),
    Field("remote_types", "Remote types", "list", ["Hybrid", "On-site"],
          "Scraper", "search",
          help="Workplace types to search, one per line (e.g. Remote, Hybrid, On-site)."),
    Field("limit_per_input", "Postings per search", "int", 100, "Scraper", "search",
          help="Max postings collected per (keyword x remote type). Higher = more spend.",
          min=1, max=500),
    Field("exclude_window_days", "Re-scrape exclusion (days)", "int", 14, "Scraper", "search",
          help="Skip postings scraped within this many days (avoids re-billing live jobs).",
          min=0, max=90),
    Field("location", "Location", "str", "United States", "Scraper", "search",
          help="Geographic location filter for searches."),
    Field("country", "Country code", "str", "US", "Scraper", "search",
          help="Two-letter country code."),
    Field("time_range", "Time range", "str", "Past 24 hours", "Scraper", "search",
          help="Posting-age filter (e.g. 'Past 24 hours', 'Past week')."),
    Field("job_type", "Job type", "str", "Full-time", "Scraper", "search",
          help="Employment type filter (e.g. 'Full-time')."),
    Field("experience_level", "Experience level", "str", "Entry level", "Scraper", "search",
          help="Seniority filter (e.g. 'Entry level')."),

    # --- Scoring: written to root-level scoring_config.json (read by score_jobs.py) ---
    Field("stage1_model", "Stage-1 model", "str", "gemini-3.1-flash-lite", "Scoring", "scoring",
          help="Cheap model that scores every surviving job 1-5."),
    Field("stage2_model", "Stage-2 model", "str", "gemini-3.5-flash", "Scoring", "scoring",
          help="Deeper model used for jobs that pass the Stage-2 threshold."),
    Field("stage1_concurrency", "Stage-1 concurrency", "int", 6, "Scoring", "scoring",
          help="Parallel Stage-1 LLM calls.", min=1, max=50),
    Field("stage2_concurrency", "Stage-2 concurrency", "int", 4, "Scoring", "scoring",
          help="Parallel Stage-2 LLM calls.", min=1, max=50),
    Field("stage2_threshold", "Stage-2 threshold", "int", 4, "Scoring", "scoring",
          help="Stage-1 score at/above which a job gets deep Stage-2 analysis.", min=1, max=5),
    Field("max_scored_per_run", "Max scored per run", "int", 800, "Scoring", "scoring",
          help="Spend guard: cap on LLM-scored jobs per run.", min=1, max=5000),
    Field("rescore_cap", "Rescore cap", "int", 200, "Scoring", "scoring",
          help="Spend guard: cap on failed/missing master rows retried per run.", min=0, max=5000),
    Field("min_filter_years", "Min required years cutoff", "int", 1, "Scoring", "scoring",
          help="Roles requiring at least this many years of experience are filtered out.",
          min=0, max=20),
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
            path = targets.get(f.target)
            cache[f.target] = _read_file(path) if path is not None else {}
        store = cache[f.target]
        values[f.key] = store[f.key] if f.key in store else f.default
    return values


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
    if f.type == "list":
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
        merged = _read_file(path)
        merged.update(updates)
        _atomic_write(Path(path), merged)
