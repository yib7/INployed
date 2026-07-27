"""Guard: the suite must never read the developer's real scrape_data/.env.

`local/app.py`, `local/resume_tailor/config.py`, `score_jobs.py` and `scraper.py`
each call `load_dotenv(<repo>/.env)` at IMPORT time. Without a guard, importing
any one of them injects that file's contents into `os.environ` for the whole
session — on the author's machine, 22 vars including `GEMINI_API_KEYS`,
`BRIGHT_DATA_API_TOKEN` and `RESUME_TAILOR_OUTPUT`.

Two things go wrong:
  1. A test asserting the keyless / unconfigured branch silently exercises the
     configured one instead, so it passes here and fails on a fresh clone (and
     in CI, where no .env exists). That is the same failure mode as the
     LOCALAPPDATA corruption — a test that only passes on one machine.
  2. `RESUME_TAILOR_OUTPUT` points `config.OUTPUT_ROOT` at the user's real
     ~/Downloads/Generated_Resumes, and `output.resolve_dir` mkdir's into it.

conftest neutralises `load_dotenv` and scrubs the keys before `local/` is even
importable. These tests fail loudly if that is ever removed.
"""
import os

import pytest


def test_load_dotenv_is_neutralised():
    dotenv = pytest.importorskip("dotenv")
    before = dict(os.environ)
    assert dotenv.load_dotenv() is False
    assert dict(os.environ) == before, "load_dotenv still mutates the environment"


def test_importing_resume_tailor_config_leaks_no_secrets():
    from resume_tailor import config  # noqa: F401  (import is the thing under test)

    for key in ("GEMINI_API_KEYS", "BRIGHT_DATA_API_TOKEN", "ANTHROPIC_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS"):
        assert key not in os.environ, (
            f"{key} reached the test environment — the real .env was loaded. "
            "See conftest's load_dotenv neutralisation."
        )


def test_output_root_is_not_the_users_real_downloads():
    from resume_tailor import config

    root = str(config.OUTPUT_ROOT).lower()
    assert "inployed-test-output" in root, (
        f"OUTPUT_ROOT is {config.OUTPUT_ROOT!r} — a tailor test would mkdir into the "
        "user's real Generated_Resumes folder."
    )
    assert "downloads" not in root or "inployed-test-output" in root


# ── env leakage between tests (found by a reverse-order suite run) ────────────

_LEAK_CANARY = "INPLOYED_LEAK_CANARY"


def test_a_test_may_dirty_the_environment():
    """Application code writes os.environ directly (MainWindow._apply_auth_env
    does exactly this), so a test can leave the environment dirty without ever
    touching monkeypatch. This test deliberately does that."""
    os.environ[_LEAK_CANARY] = "dirty"
    os.environ["RESUME_TAILOR_GEMINI_AUTH"] = "vertex"
    assert os.environ[_LEAK_CANARY] == "dirty"


def test_the_dirt_does_not_reach_the_next_test():
    """...and conftest's _restore_environ fixture must have cleaned it up.

    Without this guard, `config.gemini_auth()` (env var beats config.json) made
    test_llm_backend and test_llm_timeout pass or fail purely on whether a Qt
    test ran before them — which is why the suite passed alphabetically and
    failed in reverse file order.
    """
    assert _LEAK_CANARY not in os.environ
    assert "RESUME_TAILOR_GEMINI_AUTH" not in os.environ
