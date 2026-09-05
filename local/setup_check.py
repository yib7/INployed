"""What is missing or misconfigured, as plain problem strings ("Check setup").

Toolkit-agnostic (no Qt), so the same answers are available to the dashboard, a
future CLI, and a unit test that never starts a QApplication. The dashboard owns
only the presentation: which thread each half runs on and which dialog it lands
in (`local/qt/main_window.py`).

Two halves, split by cost rather than by topic:

- `local_problems()` — file and environment reads only, safe to call inline.
  Raises if the validators themselves fail, because "the checks could not run" is
  a different message from "the checks found something".
- `job_data_problems()` — one network probe of the job-data account, so it belongs
  on a worker thread. Free and unbilled, and silent on any failure: a setup check
  must never report a problem it did not actually observe.

The two `*_warnings` helpers are pure (all inputs passed in, no I/O) so they can
be unit-tested exhaustively. They used to live in `local/jobsdata.py` as private
functions that `main_window` reached across the module boundary to call; they are
setup-check logic rather than job-data logic, so they live here now, public.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import jobsdata
import settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def engine_credential_warnings(auth: str, project: str, has_api_key: bool) -> list[str]:
    """Warn when the chosen résumé-tailor engine is missing the credential it needs.

    'api_key' needs a Gemini API key; 'vertex' needs a Google Cloud project.
    Returns [] when the engine has what it needs.
    """
    if auth == "api_key" and not has_api_key:
        return ["Resume tailor engine is 'api_key' but no Gemini API key is saved "
                "(Settings -> Credentials -> Gemini API key (resume tailor))."]
    if auth == "vertex" and not str(project).strip():
        return ["Resume tailor engine is 'vertex' but no Google Cloud project is set "
                "(Settings -> Connection & paths -> Google Cloud project ID)."]
    return []


def claude_cli_warnings(tailor_provider: str, scoring_provider: str,
                        cli_found: bool) -> list[str]:
    """Warn when a provider is 'claude' but the CLI isn't installed. Pure (caller
    passes shutil.which('claude') is not None) so it unit-tests like
    engine_credential_warnings."""
    if cli_found:
        return []
    out = []
    if tailor_provider == "claude":
        out.append("Resume tailor provider is 'claude' but the `claude` CLI is not "
                    "on PATH. Install Claude Code and run `claude` once to log in.")
    if scoring_provider == "claude":
        out.append("Scoring provider is 'claude' but the `claude` CLI is not on "
                    "PATH -- local scoring will fall back to Gemini.")
    return out


def engine_problems() -> list[str]:
    """Credential and CLI warnings for the configured tailor + scoring providers.

    Best-effort: any failure reading config or settings returns [] rather than
    propagating, because a setup check that cannot read a file has found nothing,
    not a problem.
    """
    try:
        cfg = jobsdata._load_cfg()
        stored = settings.load()
        # Match the runtime resolvers' env > file precedence
        # (config.tailor_provider() / score_jobs.load_scoring_config()): an
        # exported RESUME_TAILOR_PROVIDER / SCORE_PROVIDER wins at run time, so
        # Check-setup must honour it too or its warnings won't match what runs.
        tailor_provider = str(
            os.environ.get("RESUME_TAILOR_PROVIDER")
            or cfg.get("tailor_provider") or "gemini").strip().lower()
        problems: list[str] = []
        if tailor_provider != "claude":  # gemini engine warnings only apply on gemini
            auth = cfg.get("gemini_auth", "vertex")
            project = stored.get("GOOGLE_CLOUD_PROJECT", "") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT", "")
            has_key = settings.secret_status().get(
                "RESUME_TAILOR_GEMINI_API_KEY", False) or bool(
                    os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"))
            problems.extend(f"[Engine] {w}" for w in
                            engine_credential_warnings(auth, project, has_key))
        scoring_provider = str(
            os.environ.get("SCORE_PROVIDER")
            or stored.get("provider") or "gemini").strip().lower()
        cli_found = shutil.which("claude") is not None
        problems.extend(f"[Engine] {w}" for w in claude_cli_warnings(
            tailor_provider, scoring_provider, cli_found))
        return problems
    except Exception:  # noqa: BLE001
        return []


def local_problems() -> list[str]:
    """Everything checkable from local files and environment, inline-safe.

    Propagates whatever `master_validate.check_setup()` raises: the caller reports
    "could not run the checks" differently from a list of findings.
    """
    from resume_tailor import master_validate
    result = master_validate.check_setup()
    problems: list[str] = []
    for label, key in (("Resume data", "master"), ("Apply answers", "answers")):
        problems.extend(f"[{label}] {e}" for e in result.get(key, []))
    problems.extend(engine_problems())
    return problems


def job_data_problems() -> list[str]:
    """Worker-thread half: can the job-data account collect?

    Free and unbilled, so the user can test Bright Data without starting a run.
    Silent whenever it can't import or reach the probe — Check setup must never
    report a problem it did not actually observe.
    """
    try:
        for _p in (str(REPO_ROOT / "pipeline"), str(REPO_ROOT / "local")):
            if _p not in sys.path:
                sys.path.insert(0, _p)
        import scraper
        return [f"[Job data] {w}" for w in scraper.account_problems()]
    except Exception:  # noqa: BLE001
        return []
