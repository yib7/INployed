"""Tests for the toolkit-agnostic "Check setup" logic (local/setup_check.py).

The two `*_warnings` truth tables cover the pure helpers; the rest covers what
the dashboard cannot reach cheaply, since anything living inside MainWindow needs
a QApplication and two mocked message boxes to test at all.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import setup_check  # noqa: E402


# --- engine_credential_warnings ------------------------------------------------

def test_engine_credential_warnings_flags_missing_api_key():
    assert setup_check.engine_credential_warnings("api_key", project="", has_api_key=False)
    assert setup_check.engine_credential_warnings("api_key", project="proj", has_api_key=True) == []


def test_engine_credential_warnings_flags_missing_vertex_project():
    assert setup_check.engine_credential_warnings("vertex", project="", has_api_key=False)
    assert setup_check.engine_credential_warnings("vertex", project="  ", has_api_key=False)  # blank
    assert setup_check.engine_credential_warnings("vertex", project="my-proj", has_api_key=False) == []


# --- claude_cli_warnings truth table -------------------------------------------

def test_claude_cli_warnings_cli_found_always_empty():
    assert setup_check.claude_cli_warnings("claude", "claude", cli_found=True) == []
    assert setup_check.claude_cli_warnings("gemini", "gemini", cli_found=True) == []


def test_claude_cli_warnings_missing_cli_tailor_claude_only():
    out = setup_check.claude_cli_warnings("claude", "gemini", cli_found=False)
    assert len(out) == 1
    assert "tailor" in out[0].lower()


def test_claude_cli_warnings_missing_cli_scoring_claude_only():
    out = setup_check.claude_cli_warnings("gemini", "claude", cli_found=False)
    assert len(out) == 1
    assert "fall back" in out[0].lower()


def test_claude_cli_warnings_missing_cli_both_claude():
    out = setup_check.claude_cli_warnings("claude", "claude", cli_found=False)
    assert len(out) == 2


def test_claude_cli_warnings_missing_cli_neither_claude():
    assert setup_check.claude_cli_warnings("gemini", "gemini", cli_found=False) == []


# --- engine_problems: env > file precedence, and best-effort silence -----------

def _stub_config(monkeypatch, cfg, stored, secrets=None, claude_on_path=False):
    monkeypatch.setattr(setup_check.jobsdata, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(setup_check.settings, "load", lambda: stored)
    monkeypatch.setattr(setup_check.settings, "secret_status", lambda: secrets or {})
    monkeypatch.setattr(setup_check.shutil, "which",
                        lambda name: "/usr/bin/claude" if claude_on_path else None)
    for var in ("RESUME_TAILOR_PROVIDER", "SCORE_PROVIDER",
                "GOOGLE_CLOUD_PROJECT", "RESUME_TAILOR_GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_engine_problems_clean_when_vertex_has_a_project(monkeypatch):
    _stub_config(monkeypatch, {"gemini_auth": "vertex"}, {"GOOGLE_CLOUD_PROJECT": "proj"})
    assert setup_check.engine_problems() == []


def test_engine_problems_flags_vertex_without_a_project(monkeypatch):
    _stub_config(monkeypatch, {"gemini_auth": "vertex"}, {})
    out = setup_check.engine_problems()
    assert len(out) == 1
    assert out[0].startswith("[Engine] ")
    assert "Google Cloud project" in out[0]


def test_engine_problems_env_provider_beats_the_file(monkeypatch):
    """The runtime resolvers use env > file precedence, so this must too: both
    files say gemini, the environment says claude, and no CLI is on PATH."""
    _stub_config(monkeypatch, {"tailor_provider": "gemini", "gemini_auth": "vertex"},
                 {"provider": "gemini"})
    monkeypatch.setenv("RESUME_TAILOR_PROVIDER", "claude")
    out = setup_check.engine_problems()
    assert any("tailor provider is 'claude'" in w for w in out)
    # A claude tailor must NOT also raise the gemini vertex-project warning.
    assert not any("Google Cloud project" in w for w in out)


def test_engine_problems_is_silent_when_config_cannot_be_read(monkeypatch):
    """A check that cannot read a file has found nothing, not a problem."""
    def boom():
        raise OSError("config.json is locked")
    monkeypatch.setattr(setup_check.jobsdata, "_load_cfg", boom)
    assert setup_check.engine_problems() == []


# --- local_problems: labelling, and the "could not run" path ------------------

def test_local_problems_labels_each_validator(monkeypatch):
    from resume_tailor import master_validate
    monkeypatch.setattr(master_validate, "check_setup",
                        lambda: {"master": ["no name"], "answers": ["no email"]})
    monkeypatch.setattr(setup_check, "engine_problems", lambda: [])
    assert setup_check.local_problems() == ["[Resume data] no name", "[Apply answers] no email"]


def test_local_problems_propagates_a_validator_failure(monkeypatch):
    """"The checks could not run" is a different message from "the checks found
    something", so this half must raise rather than return []."""
    import pytest
    from resume_tailor import master_validate

    def boom():
        raise RuntimeError("master_experience.yaml is unreadable")
    monkeypatch.setattr(master_validate, "check_setup", boom)
    with pytest.raises(RuntimeError):
        setup_check.local_problems()


# --- job_data_problems: the path hop, and the never-invent rule ----------------

def test_repo_root_resolves_to_the_tree_that_holds_pipeline_and_local():
    """setup_check.py sits one level below the root (it moved down from
    local/qt/main_window.py, which was two), so the hop count is a real hazard."""
    assert (setup_check.REPO_ROOT / "pipeline").is_dir()
    assert (setup_check.REPO_ROOT / "local" / "setup_check.py").is_file()


def test_job_data_problems_never_invents_a_problem(monkeypatch):
    """The probe runs on a worker thread and must swallow anything the import or
    the network throws — a setup check must not report what it did not observe."""
    monkeypatch.setitem(sys.modules, "scraper", None)  # attribute access raises
    assert setup_check.job_data_problems() == []
