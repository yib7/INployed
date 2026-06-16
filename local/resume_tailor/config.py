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
# flash       → selection+skills, fact-verify, shrink, AND the constrained fix-up
#               passes (rephrase_fix / refit) — these only rewrite already-good
#               text to fix grounding or hit a line length, so flash matches PRO
#               quality at ~5x lower cost.
# pro         → the creative first pass (rephrase) + cover letter (quality-critical)
MODEL_FLASH = os.getenv("RESUME_TAILOR_MODEL_FLASH", "gemini-2.5-flash")
# Pro tier: this project exposes gemini-3.1-pro-preview (3.5-pro/3.1-pro 404 here).
MODEL_PRO = os.getenv("RESUME_TAILOR_MODEL_PRO", "gemini-3.1-pro-preview")
# flash-lite → cheapest tier for high-frequency, low-stakes classification (the
# master_experience JD-gap screen/placement). Quality isn't critical and edits are
# reviewed before any write, so the cheapest model is the right call.
MODEL_FLASH_LITE = os.getenv("RESUME_TAILOR_MODEL_FLASH_LITE", "gemini-2.5-flash-lite")

# ── pdflatex ─────────────────────────────────────────────────────────────────
PDFLATEX_PATH = os.getenv("PDFLATEX_PATH", "pdflatex")
PAGE_LIMIT = 1
# The deterministic drop-weakest-bullet step usually converges the page well
# before this; 3 flash shrink passes is plenty (was 4).
MAX_SHRINK_ATTEMPTS = 3

# ── Backend selection (Vertex Gemini vs local Claude CLI) ────────────────────
# Resolved at call time (not cached) so the long-lived dashboard picks up a
# toggle without restart. Precedence: env var > local/config.json > "vertex".
CONFIG_JSON = PKG_DIR.parent / "config.json"            # local/config.json

TIER_FLASH_LITE = "flash_lite"
TIER_FLASH = "flash"
TIER_PRO = "pro"

# Claude tier targets (CLI model aliases). pro reuses sonnet — no opus tier.
CLAUDE_HAIKU = os.getenv("RESUME_TAILOR_CLAUDE_HAIKU", "haiku")
CLAUDE_SONNET = os.getenv("RESUME_TAILOR_CLAUDE_SONNET", "sonnet")

_VERTEX_TIERS = {
    TIER_FLASH_LITE: MODEL_FLASH_LITE,
    TIER_FLASH: MODEL_FLASH,
    TIER_PRO: MODEL_PRO,
}
_CLAUDE_TIERS = {
    TIER_FLASH_LITE: CLAUDE_HAIKU,
    TIER_FLASH: CLAUDE_SONNET,
    TIER_PRO: CLAUDE_SONNET,
}


def _config_json() -> dict:
    """local/config.json (shared with the dashboard), {} when unreadable."""
    try:
        return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def backend() -> str:
    """Active LLM backend: 'vertex' (default) or 'claude'."""
    val = os.getenv("RESUME_TAILOR_BACKEND") or _config_json().get("backend")
    val = (val or "vertex").strip().lower()
    return val if val in ("vertex", "claude") else "vertex"


def model_for(tier: str) -> str:
    """Concrete model for a tier token under the active backend."""
    table = _CLAUDE_TIERS if backend() == "claude" else _VERTEX_TIERS
    return table[tier]
