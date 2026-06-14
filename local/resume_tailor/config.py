"""Paths, model tiers, and Vertex settings for the resume tailor.

Everything is env-overridable so the flash-lite / flash / pro split can be
re-tuned against the $300 credit without code changes.
"""
from __future__ import annotations

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
VERBS_PDF = ASSETS_DIR / "active-verb-list-final.pdf"
# Style exemplar fed (bounded) into the rephrase prompt — the one-page look the user likes.
EXAMPLE_PDF = ASSETS_DIR / "resume_sample.pdf"
VERBS_TXT_CACHE = ASSETS_DIR / "active_verbs.txt"      # extracted once, then reused

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

# ── pdflatex ─────────────────────────────────────────────────────────────────
PDFLATEX_PATH = os.getenv("PDFLATEX_PATH", "pdflatex")
PAGE_LIMIT = 1
# The deterministic drop-weakest-bullet step usually converges the page well
# before this; 3 flash shrink passes is plenty (was 4).
MAX_SHRINK_ATTEMPTS = 3
