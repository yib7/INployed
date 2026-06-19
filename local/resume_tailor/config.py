"""Paths, model tiers, and Vertex settings for the resume tailor.

Everything is env-overridable so the flash-lite / flash / pro split can be
re-tuned against the $300 credit without code changes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ── Locations ────────────────────────────────────────────────────────────────
PKG_DIR = Path(__file__).resolve().parent              # local/resume_tailor
SCRAPE_DIR = PKG_DIR.parent.parent                     # scrape_data

# Optional: load scrape_data/.env so GCP project / candidate / keys live outside
# the repo. Missing python-dotenv is fine — env vars still work without it.
try:
    from dotenv import load_dotenv

    load_dotenv(SCRAPE_DIR / ".env")
except ImportError:
    pass
ASSETS_DIR = SCRAPE_DIR / "resume_tailor_files"

MASTER_YAML = ASSETS_DIR / "master_experience.yaml"
TEMPLATE_TEX = ASSETS_DIR / "resume_template.tex"
# Style exemplar fed (bounded) into the rephrase prompt — the one-page look the user likes.
EXAMPLE_PDF = ASSETS_DIR / "resume_sample.pdf"

OUTPUT_ROOT = Path(os.getenv("RESUME_TAILOR_OUTPUT", str(Path.home() / "Downloads" / "Generated_Resumes")))
CANDIDATE_NAME = os.getenv("RESUME_TAILOR_CANDIDATE", "Your_Name")

# ── Vertex AI ────────────────────────────────────────────────────────────────
# Mirrors score_jobs.py: ADC + GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION.
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

# ── Per-task model tiers ─────────────────────────────────────────────────────
# flash       → the judgment passes: selection (which evidence), fact-verify
#               (anti-inflation audit), and the skills fallback. Few calls/run.
# pro         → the creative first pass (rephrase) + cover letter (quality-critical)
MODEL_FLASH = os.getenv("RESUME_TAILOR_MODEL_FLASH", "gemini-3.5-flash")
# PRO tier maps to 3.5-flash by default; set this to gemini-3.1-pro-preview for
# max-quality (slower) rephrase + cover-letter passes.
MODEL_PRO = os.getenv("RESUME_TAILOR_MODEL_PRO", "gemini-3.5-flash")
MODEL_FLASH_LITE = os.getenv("RESUME_TAILOR_MODEL_FLASH_LITE", "gemini-3.1-flash-lite")

# ── pdflatex ─────────────────────────────────────────────────────────────────
PDFLATEX_PATH = os.getenv("PDFLATEX_PATH", "pdflatex")
PAGE_LIMIT = 1
# ── Auth / model selection ────────────────────────────────────────────────────
# Resolved at call time. Precedence: env var > local/config.json > default.
CONFIG_JSON = PKG_DIR.parent / "config.json"            # local/config.json

TIER_FLASH_LITE = "flash_lite"
TIER_FLASH = "flash"
TIER_PRO = "pro"

_GEMINI_TIERS = {
    TIER_FLASH_LITE: MODEL_FLASH_LITE,
    TIER_FLASH: MODEL_FLASH,
    TIER_PRO: MODEL_PRO,
}


def _config_json() -> dict:
    """local/config.json (shared with the dashboard), {} when unreadable."""
    try:
        return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── Resume layout (per-bullet line targets for the constant blocks) ───────────
# Editable from the dashboard; persisted in local/config.json under "resume_layout"
# as {block_name: {"line_targets": [int, ...]}}. The list length is the bullet
# count for that block; each int is that bullet's printed-line target, which drives
# the soft length hint in rephrase and the deterministic trim cap (lines * MAX_LINE_CHARS).
MAX_LINE_CHARS = int(os.getenv("RESUME_TAILOR_MAX_LINE_CHARS", "100"))
DEFAULT_LINE_TARGETS = [2, 2, 2]
PROJECTS_MAX = int(os.getenv("RESUME_TAILOR_PROJECTS_MAX", "3"))
PROJECT_BULLETS_MAX = int(os.getenv("RESUME_TAILOR_PROJECT_BULLETS_MAX", "2"))
PROJECT_BULLET_LINES = int(os.getenv("RESUME_TAILOR_PROJECT_BULLET_LINES", "2"))


def resume_layout() -> dict:
    """Raw {block: {'line_targets': [...]}} from config.json ({} when absent/bad)."""
    val = _config_json().get("resume_layout")
    return val if isinstance(val, dict) else {}


def block_targets(name: str) -> list[int]:
    """Sanitized per-bullet line targets for a constant block. config.json value
    (ints clamped 1-3, list length clamped 1-5) else DEFAULT_LINE_TARGETS."""
    spec = resume_layout().get(name)
    raw = spec.get("line_targets") if isinstance(spec, dict) else None
    if not isinstance(raw, (list, tuple)) or not raw:
        return list(DEFAULT_LINE_TARGETS)
    out: list[int] = []
    for t in raw[:5]:
        try:
            out.append(max(1, min(3, int(t))))
        except (TypeError, ValueError):
            return list(DEFAULT_LINE_TARGETS)
    return out or list(DEFAULT_LINE_TARGETS)


def gemini_auth() -> str:
    """Gemini auth mode: 'vertex' (default; uses GOOGLE_CLOUD_PROJECT) or
    'api_key' (uses RESUME_TAILOR_GEMINI_API_KEY -- for users without Vertex).
    Precedence: env var > local/config.json > 'vertex'."""
    val = os.getenv("RESUME_TAILOR_GEMINI_AUTH") or _config_json().get("gemini_auth")
    val = val.strip().lower() if isinstance(val, str) else "vertex"
    return "api_key" if val == "api_key" else "vertex"


def model_for(tier: str) -> str:
    """Concrete Gemini model id for a tier token."""
    return _GEMINI_TIERS.get(tier, MODEL_FLASH)
