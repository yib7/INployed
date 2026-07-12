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
# Curated, categorized résumé action verbs the rephrase pass draws openers from (one per
# bullet, never reused). Universal (not personal), so it is tracked; built-in fallback if absent.
ACTIVE_WORDS_MD = ASSETS_DIR / "active_words.md"

OUTPUT_ROOT = Path(os.getenv("RESUME_TAILOR_OUTPUT", str(Path.home() / "Downloads" / "Generated_Resumes")))
CANDIDATE_NAME = os.getenv("RESUME_TAILOR_CANDIDATE", "Your_Name")

# ── Vertex AI ────────────────────────────────────────────────────────────────
# Mirrors score_jobs.py: ADC + GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION.
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
GCP_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

# ── Per-task model tiers ─────────────────────────────────────────────────────
# flash       → the judgment passes: selection (which evidence) and the skills
#               fallback. Few calls/run.
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

# Tier token -> (env var, default) for model_for()'s live lookup.
_TIER_ENV = {
    TIER_FLASH_LITE: ("RESUME_TAILOR_MODEL_FLASH_LITE", MODEL_FLASH_LITE),
    TIER_FLASH: ("RESUME_TAILOR_MODEL_FLASH", MODEL_FLASH),
    TIER_PRO: ("RESUME_TAILOR_MODEL_PRO", MODEL_PRO),
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
MAX_LINE_CHARS = int(os.getenv("RESUME_TAILOR_MAX_LINE_CHARS", "130"))
DEFAULT_LINE_TARGETS = [2, 2, 2]
PROJECTS_MAX = int(os.getenv("RESUME_TAILOR_PROJECTS_MAX", "3"))  # built-in default / fallback
PROJECTS_MAX_LIMIT = 6  # hard ceiling for the configurable cap: the resume is one page.
PROJECT_BULLETS_MAX = int(os.getenv("RESUME_TAILOR_PROJECT_BULLETS_MAX", "2"))
PROJECT_BULLET_LINES = int(os.getenv("RESUME_TAILOR_PROJECT_BULLET_LINES", "2"))


def _clamp_projects(n: int) -> int:
    """Keep a projects cap in 1..PROJECTS_MAX_LIMIT so a bad value can't blow up the
    one-page layout (0 projects, or 50)."""
    return max(1, min(PROJECTS_MAX_LIMIT, n))


def projects_max() -> int:
    """Effective cap on how many projects the tailored resume lists.

    Resolved live (not frozen at import) so the dashboard's Resume setting takes
    effect without a restart. Precedence: RESUME_TAILOR_PROJECTS_MAX env var >
    config.json 'projects_max' (the GUI value) > the built-in default PROJECTS_MAX.
    Always clamped to 1..PROJECTS_MAX_LIMIT."""
    env = os.getenv("RESUME_TAILOR_PROJECTS_MAX")
    if env is not None and str(env).strip():
        try:
            return _clamp_projects(int(env))
        except ValueError:
            pass
    try:
        return _clamp_projects(int(_config_json().get("projects_max")))
    except (TypeError, ValueError):
        return _clamp_projects(PROJECTS_MAX)


def projects_mode() -> str:
    """How projects_max() is applied during one-page enforcement:

      'max'   -> at most N projects; the weakest project bullets (and, if needed,
                 whole projects) are dropped to hold one page.
      'exact' -> keep exactly N projects (when that many are available): one-page
                 enforcement may trim a project's extra bullets but never drops a
                 project's LAST bullet, so the count holds (best-effort if it still
                 overflows a page).

    Precedence: RESUME_TAILOR_PROJECTS_MODE env var > config.json 'projects_mode'
    (the GUI value) > 'max'. Anything other than 'exact' resolves to 'max'."""
    val = os.getenv("RESUME_TAILOR_PROJECTS_MODE") or _config_json().get("projects_mode")
    val = val.strip().lower() if isinstance(val, str) else "max"
    return "exact" if val == "exact" else "max"


def fill_underfull_enabled() -> bool:
    """Whether the underfull-bullet fill pass runs (compose.fill_underfull): when a tailored
    bullet renders shorter than its configured line target, fold ONE detail from an unused atom
    in the SAME block in to fill it. It never fabricates -- a bullet whose block has no spare
    atom is left exactly as-is. Defaults ON. Precedence: RESUME_TAILOR_FILL_UNDERFULL env >
    config.json 'fill_underfull' > True."""
    env = os.getenv("RESUME_TAILOR_FILL_UNDERFULL")
    if env is not None and str(env).strip():
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    return _config_json().get("fill_underfull", True) is not False


def lead_overview_enabled() -> bool:
    """Whether the project-overview-first reorder runs (compose.lead_with_overview): float
    each project's overview/intro bullet ("what is this project at a glance") to the front so
    detail bullets don't lead. select() orders bullets by JD-relevance, which can bury the
    overview; this pass picks the lead via a cheap model call with a deterministic file-order
    fallback (the master authors the overview atom first), so flow is always enforced. It only
    REORDERS existing bullets -- never invents. Defaults ON. Precedence: RESUME_TAILOR_LEAD_OVERVIEW
    env > config.json 'lead_overview' > True."""
    env = os.getenv("RESUME_TAILOR_LEAD_OVERVIEW")
    if env is not None and str(env).strip():
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    return _config_json().get("lead_overview", True) is not False


def methods_line_enabled() -> bool:
    """Whether the résumé renders a 'Methods' concepts line (compose.methods_line): a 5th
    technical-skills line that surfaces the JD's concept buzzwords ('A/B Testing', 'ETL',
    'data wrangling') the candidate genuinely owns — printed in the JD's own spelling via
    the anchored skill_aliases layer (Tier 1) then padded with the model's role-relevant
    concept ranking (Tier 2). It only draws from concepts_and_methodologies the user
    declared, never invents. Defaults ON. Precedence: RESUME_TAILOR_METHODS_LINE env >
    config.json 'methods_line' > True."""
    env = os.getenv("RESUME_TAILOR_METHODS_LINE")
    if env is not None and str(env).strip():
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    return _config_json().get("methods_line", True) is not False


def methods_line_label() -> str:
    """The label printed before the Methods concepts line. Precedence:
    RESUME_TAILOR_METHODS_LABEL env > config.json 'methods_line_label' > 'Methods'."""
    env = os.getenv("RESUME_TAILOR_METHODS_LABEL")
    if env is not None and str(env).strip():
        return str(env).strip()
    val = _config_json().get("methods_line_label")
    return str(val).strip() if isinstance(val, str) and str(val).strip() else "Methods"


def tech_aliases_enabled() -> bool:
    """Whether the four technical-skills lines swap a printed skill to the JD's own spelling
    when the JD uses a PRINTABLE alias of it (compose._finalize_skill_lines): e.g. a JD that
    says 'Postgres' makes the line print 'Postgres' instead of 'PostgreSQL', so a literal
    keyword ATS sees the JD's exact term. Only printable skill_aliases swap; match-only
    synonyms never do. Off lets a user who dislikes an abbreviation swap (Kubernetes -> K8s)
    keep their canonical spellings. Defaults ON. Precedence: RESUME_TAILOR_TECH_ALIASES env >
    config.json 'tech_aliases' > True."""
    env = os.getenv("RESUME_TAILOR_TECH_ALIASES")
    if env is not None and str(env).strip():
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    return _config_json().get("tech_aliases", True) is not False


def resume_layout_enabled() -> bool:
    """Master on/off for the custom bullet layout (config.json `resume_layout_enabled`).
    Defaults True when absent, so existing configs keep applying their saved targets.
    When False, `resume_layout()`/`project_layout()` report empty so the engine falls
    back to its built-in defaults WITHOUT the saved targets being deleted -- a quick
    A/B test of custom-vs-default layout."""
    return _config_json().get("resume_layout_enabled", True) is not False


def resume_layout() -> dict:
    """Raw {block: {'line_targets': [...]}} from config.json ({} when absent/bad or
    when the master toggle is off)."""
    if not resume_layout_enabled():
        return {}
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


def verbatim_blocks() -> dict:
    """{block_name: [bullet, ...]} for blocks the user marked 'don't tailor — use my
    exact bullets'. A block with a non-empty list renders those exact bullets and the
    LLM is bypassed for it; an empty/absent list means normal tailoring. Returns
    sanitized {name: [non-empty str, ...]} ({} when absent/bad). NOT gated by the
    resume_layout master toggle — verbatim is an explicit per-block override."""
    val = _config_json().get("verbatim_blocks")
    if not isinstance(val, dict):
        return {}
    out: dict = {}
    for name, bullets in val.items():
        if isinstance(bullets, (list, tuple)):
            clean = [str(b).strip() for b in bullets if str(b).strip()]
            if clean:
                out[str(name)] = clean
    return out


def project_layout() -> dict:
    """Raw {project_name: {'line_targets': [...]}} from config.json ({} when absent/bad
    or when the master toggle is off)."""
    if not resume_layout_enabled():
        return {}
    val = _config_json().get("project_layout")
    return val if isinstance(val, dict) else {}


def project_targets(name: str) -> list[int] | None:
    """Sanitized per-bullet line targets for a project (ints clamped 1-3, list length
    clamped 1-5). Returns None when this project is NOT configured, so callers fall
    back to the global PROJECT_BULLETS_MAX / PROJECT_BULLET_LINES."""
    spec = project_layout().get(name)
    raw = spec.get("line_targets") if isinstance(spec, dict) else None
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out: list[int] = []
    for t in raw[:5]:
        try:
            out.append(max(1, min(3, int(t))))
        except (TypeError, ValueError):
            return None
    return out or None


def project_bullet_tiers() -> list[int] | None:
    """Tiered, RANK-based project bullet counts expanded into a per-rank list.

    config.json `project_bullet_tiers` is a list of {"projects": N, "bullets": M}
    tier objects read top-down against the strength-ranked project list (select()
    orders strongest-first). [{projects:2,bullets:3},{projects:2,bullets:2},
    {projects:1,bullets:1}] -> [3, 3, 2, 2, 1]: the top 2 projects get 3 bullets,
    the next 2 get 2, the 5th gets 1. Projects past the last tier fall back to the
    global PROJECT_BULLETS_MAX (callers use project_rank_bullets, which returns None
    there). `projects` is clamped >=1, `bullets` clamped 1-5 (matching the
    project_targets list-length range); a tier missing/garbling either key is skipped.
    The expanded list is capped at PROJECTS_MAX_LIMIT. Returns None when absent/bad or
    when the resume_layout master toggle is off, so this is opt-in and gated like
    project_layout / resume_layout."""
    if not resume_layout_enabled():
        return None
    raw = _config_json().get("project_bullet_tiers")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out: list[int] = []
    for tier in raw:
        if not isinstance(tier, dict):
            continue
        try:
            count = max(1, int(tier["projects"]))
            bullets = max(1, min(5, int(tier["bullets"])))
        except (KeyError, TypeError, ValueError):
            continue
        out.extend([bullets] * count)
        if len(out) >= PROJECTS_MAX_LIMIT:
            break
    out = out[:PROJECTS_MAX_LIMIT]
    return out or None


def project_rank_bullets(rank: int) -> int | None:
    """Bullet count for the project at 0-based strength `rank` from the configured
    tiers, or None when no tiers are configured or `rank` is past the last tier (the
    caller then uses the global PROJECT_BULLETS_MAX)."""
    tiers = project_bullet_tiers()
    if not tiers or rank < 0 or rank >= len(tiers):
        return None
    return tiers[rank]


def gemini_auth() -> str:
    """Gemini auth mode: 'vertex' (default; uses GOOGLE_CLOUD_PROJECT) or
    'api_key' (uses RESUME_TAILOR_GEMINI_API_KEY -- for users without Vertex).
    Precedence: env var > local/config.json > 'vertex'."""
    val = os.getenv("RESUME_TAILOR_GEMINI_AUTH") or _config_json().get("gemini_auth")
    val = val.strip().lower() if isinstance(val, str) else "vertex"
    return "api_key" if val == "api_key" else "vertex"


def tailor_provider() -> str:
    """'gemini' (default) or 'claude'. Live: env > local/config.json > 'gemini'."""
    val = os.getenv("RESUME_TAILOR_PROVIDER") or _config_json().get("tailor_provider")
    val = val.strip().lower() if isinstance(val, str) else "gemini"
    return "claude" if val == "claude" else "gemini"


CLAUDE_MODEL_FLASH_LITE = os.getenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", "claude-haiku-4-5")
CLAUDE_MODEL_FLASH      = os.getenv("RESUME_TAILOR_CLAUDE_MODEL_FLASH", "claude-sonnet-5")
CLAUDE_MODEL_PRO        = os.getenv("RESUME_TAILOR_CLAUDE_MODEL_PRO", "claude-opus-4-8")

_CLAUDE_TIER_ENV = {
    TIER_FLASH_LITE: ("RESUME_TAILOR_CLAUDE_MODEL_FLASH_LITE", CLAUDE_MODEL_FLASH_LITE),
    TIER_FLASH:      ("RESUME_TAILOR_CLAUDE_MODEL_FLASH", CLAUDE_MODEL_FLASH),
    TIER_PRO:        ("RESUME_TAILOR_CLAUDE_MODEL_PRO", CLAUDE_MODEL_PRO),
}


def claude_model_for(tier: str) -> str:
    """Concrete Claude model for a tier, resolved live like model_for()
    (config.py:330). Unknown tier falls back to the flash (sonnet) model."""
    env, default = _CLAUDE_TIER_ENV.get(tier, (None, CLAUDE_MODEL_FLASH))
    return os.getenv(env, default) if env else default


def _parse_timeouts(raw: str, default: list[int]) -> list[int]:
    """Parse comma-separated timeout values from a string. Returns default if
    parsing fails or produces no positive values."""
    if not raw or not raw.strip():
        return default
    try:
        vals = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default
    vals = [v for v in vals if v > 0]
    return vals if vals else default


def claude_timeout_schedule() -> list[int]:
    """Escalating per-attempt Claude CLI timeouts, default [180, 300] (CLI
    cold-start + opus latency; Gemini's 60s first slot would burn attempts).
    Override RESUME_TAILOR_CLAUDE_TIMEOUTS='180,300'; garbage falls back."""
    raw = os.getenv("RESUME_TAILOR_CLAUDE_TIMEOUTS", "")
    return _parse_timeouts(raw, [180, 300])


def model_for(tier: str) -> str:
    """Concrete Gemini model id for a tier token.

    Resolved live (not frozen at import), like gemini_auth()/projects_max() --
    reads os.environ on every call rather than caching a module-level constant.
    That does NOT mean a Settings-written RESUME_TAILOR_MODEL_* change in .env
    is picked up by an already-running dashboard: load_dotenv() only runs once,
    at import, and Settings writes to the .env file, not to os.environ directly.
    The new value is only visible to a process that re-reads env after the
    write -- e.g. the dashboard restarting, or the var being set directly in
    the environment (not just the .env file) before the process starts. An
    unrecognized tier falls back to the flash model."""
    env, default = _TIER_ENV.get(tier, (None, MODEL_FLASH))
    return os.getenv(env, default) if env else default


def tailor_timeout_schedule() -> list[int]:
    """Per-attempt Gemini-call timeouts in seconds, escalating. Default 60/120/180.

    Each entry is one attempt's timeout; on a timeout the next (longer) value is
    tried, and after the last the call fails with a timeout error. So the list
    length is also the max number of attempts. Override with
    RESUME_TAILOR_TIMEOUTS='60,120,180'; garbage / non-positive / empty falls back
    to the default. Not a Settings GUI field on purpose — a value a non-technical
    user could set to 0 is a footgun (see DECISIONS); power users still get the env."""
    raw = os.getenv("RESUME_TAILOR_TIMEOUTS", "")
    return _parse_timeouts(raw, [60, 120, 180])
