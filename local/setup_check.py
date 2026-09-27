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
be unit-tested exhaustively. They are public here rather than private in
`local/jobsdata.py`, where `main_window` had to reach across a module boundary to
call them: this is setup-check logic, not job-data logic.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

import jobsdata
import settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def engine_credential_warnings(auth: str, project: str, has_api_key: bool,
                               has_pool_keys: bool = False) -> list[str]:
    """Warn when the chosen résumé-tailor engine is missing the credential it needs.

    'api_key' needs a Gemini API key; 'vertex' needs a Google Cloud project;
    'pool' needs either the scorer's Gemini API keys or a project (the same rule
    as llm._check_creds, so this warning matches the run-time failure).
    Returns [] when the engine has what it needs.
    """
    if auth == "api_key" and not has_api_key:
        return ["Resume tailor engine is 'api_key' but no Gemini API key is saved "
                "(Settings -> Credentials -> Gemini API key (resume tailor))."]
    if auth == "vertex" and not str(project).strip():
        return ["Resume tailor engine is 'vertex' but no Google Cloud project is set "
                "(Settings -> Connection & paths -> Google Cloud project ID)."]
    if auth == "pool" and not has_pool_keys and not str(project).strip():
        return ["Resume tailor engine is 'pool' but no Gemini API keys are saved and no "
                "Google Cloud project is set (Settings -> Credentials -> Gemini API keys "
                "(job scorer), or Connection & paths -> Google Cloud project ID)."]
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
            secrets = settings.secret_status()
            has_key = secrets.get("RESUME_TAILOR_GEMINI_API_KEY", False) or bool(
                os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"))
            has_pool = secrets.get("GEMINI_API_KEYS", False) or bool(
                os.environ.get("GEMINI_API_KEYS", "").strip()
                or os.environ.get("GEMINI_API_KEY", "").strip())
            problems.extend(f"[Engine] {w}" for w in
                            engine_credential_warnings(auth, project, has_key, has_pool))
        scoring_provider = str(
            os.environ.get("SCORE_PROVIDER")
            or stored.get("provider") or "gemini").strip().lower()
        cli_found = shutil.which("claude") is not None
        problems.extend(f"[Engine] {w}" for w in claude_cli_warnings(
            tailor_provider, scoring_provider, cli_found))
        return problems
    except Exception:  # noqa: BLE001
        return []


# --- Auto-apply (cycle 16): the Jev judge and the Playwright browser -------------

def auto_apply_warnings(has_key: bool, jev_mode: str, sdk_found: bool,
                        playwright_found: bool, chromium_found: bool) -> list[str]:
    """Warn when an auto-apply run would refuse to start. Pure, like the two
    truth tables above.

    The key and the SDK only matter in 'typesafe' mode: 'fake' is the test-only
    judge and needs neither. Playwright and Chromium are needed in every mode.
    A missing Playwright package folds the Chromium row into its own line,
    since `playwright install chromium` cannot run without it.
    """
    out: list[str] = []
    if jev_mode == "typesafe":
        if not has_key:
            out.append("Auto-apply judge is 'typesafe' but no TypeSafe API key is saved. "
                       "Create one at console.typesafe.ai/keys and paste it into "
                       "Settings -> Jev -> TypeSafe API key (Jev judge).")
        if not sdk_found:
            out.append("The typesafe_sdk package is not installed: run "
                       "`pip install typesafe-sdk` (it is in requirements.txt).")
    if not playwright_found:
        out.append("Playwright is not installed, and auto-apply runs drive a Playwright "
                   "Chromium: run `pip install playwright`, then "
                   "`playwright install chromium`.")
    elif not chromium_found:
        out.append("Playwright is installed but its Chromium is not: run "
                   "`playwright install chromium`.")
    return out


def module_found(name: str) -> bool:
    """Is `name` importable? A find_spec probe, so nothing is imported (the
    Playwright package starts a driver on import and the SDK pulls pydantic)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def chromium_cache_dirs() -> list[Path]:
    """Where a Playwright Chromium build would live, in probe order.

    `PLAYWRIGHT_BROWSERS_PATH` overrides the default location when set: `0`
    means the browsers live inside the installed playwright package itself
    (`driver/package/.local-browsers`), resolved through `find_spec` so
    nothing is imported; any other value is that directory outright. Without
    the override the default is platform-specific: `ms-playwright` under
    %LOCALAPPDATA% on Windows, `~/Library/Caches/ms-playwright` on macOS,
    `~/.cache/ms-playwright` elsewhere.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override:
        if override != "0":
            return [Path(override)]
        try:
            spec = importlib.util.find_spec("playwright")
        except (ImportError, ValueError):
            spec = None
        if spec is None or not spec.submodule_search_locations:
            return []
        pkg_dir = Path(next(iter(spec.submodule_search_locations)))
        return [pkg_dir / "driver" / "package" / ".local-browsers"]
    if sys.platform == "win32":
        lad = os.environ.get("LOCALAPPDATA", "").strip()
        return [Path(lad) / "ms-playwright"] if lad else []
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "ms-playwright"]
    return [Path.home() / ".cache" / "ms-playwright"]


def chromium_installed(cache_dirs: Iterable[Path] | None = None) -> bool:
    """Is a Chromium build present in a Playwright browsers cache dir?

    A file and environment read only, so it is safe on the UI thread: no
    subprocess. `playwright` itself must be importable (`module_found`, a
    `find_spec` probe) and one of the cache dirs must hold a subdirectory
    whose name starts with "chromium" (the shape `playwright install
    chromium` produces, e.g. `chromium-1181`). A missing or unreadable cache
    dir reads as False, since a browser the probe cannot see is one a run
    cannot launch either. `cache_dirs` is injectable so a test can fake it
    without a real install.
    """
    if not module_found("playwright"):
        return False
    dirs = list(cache_dirs) if cache_dirs is not None else chromium_cache_dirs()
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            if any(p.name.startswith("chromium") for p in d.iterdir() if p.is_dir()):
                return True
        except OSError:
            continue
    return False


def chrome_install_paths() -> list[Path]:
    """Where an installed Google Chrome executable lives, per platform."""
    if sys.platform == "win32":
        roots = [os.environ.get(k, "").strip()
                 for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return [Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe" for r in roots if r]
    if sys.platform == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    return [Path("/opt/google/chrome/chrome"), Path("/usr/bin/google-chrome")]


def chrome_installed(paths: Iterable[Path] | None = None) -> bool:
    """Is Google Chrome installed? A file check only, safe on the UI thread.

    The auto-apply run launches Chrome first (`apply_run.launch_profile`) and
    falls back to the bundled Playwright Chromium, so either one is enough."""
    for p in (list(paths) if paths is not None else chrome_install_paths()):
        try:
            if p.is_file():
                return True
        except OSError:
            continue
    return False


def auto_apply_problems() -> list[str]:
    """Key, SDK, Playwright and Chromium rows for the Jev-judged auto-apply run.

    Best-effort like `engine_problems`: a failure to read settings returns [].
    The key counts as present from either the saved .env or the live
    environment, the same two places `jev.TypeSafeJev` looks.
    """
    try:
        stored = settings.load()
        jev_mode = str(stored.get("auto_apply_jev_mode") or "typesafe").strip().lower()
        has_key = settings.secret_status().get("TYPESAFE_API_KEY", False) or bool(
            os.environ.get("TYPESAFE_API_KEY", "").strip())
        playwright_found = module_found("playwright")
        chromium_found = playwright_found and (chrome_installed() or chromium_installed())
        return [f"[Auto-apply] {w}" for w in auto_apply_warnings(
            has_key, jev_mode, module_found("typesafe_sdk"),
            playwright_found, chromium_found)]
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
    problems.extend(auto_apply_problems())
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
