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


def test_engine_credential_warnings_pool_needs_keys_or_a_project():
    # Mirrors llm._check_creds: either lane on its own is a usable pool.
    assert setup_check.engine_credential_warnings("pool", project="", has_api_key=False)
    assert setup_check.engine_credential_warnings("pool", project="", has_api_key=True)  # tailor key is not the pool
    assert setup_check.engine_credential_warnings("pool", project="", has_api_key=False, has_pool_keys=True) == []
    assert setup_check.engine_credential_warnings("pool", project="my-proj", has_api_key=False) == []


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
    monkeypatch.setattr(setup_check, "auto_apply_problems", lambda: [])
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


# --- auto_apply_warnings truth table (cycle 16) ---------------------------------

def _aa(**kw):
    base = dict(has_key=True, jev_mode="typesafe", sdk_found=True,
                playwright_found=True, chromium_found=True)
    base.update(kw)
    return setup_check.auto_apply_warnings(**base)


def test_auto_apply_warnings_all_present_is_empty():
    assert _aa() == []


def test_auto_apply_warnings_missing_key_names_the_console_and_the_settings_row():
    out = _aa(has_key=False)
    assert len(out) == 1
    assert "console.typesafe.ai/keys" in out[0]
    assert "Settings -> Auto-apply" in out[0]


def test_auto_apply_warnings_fake_mode_needs_neither_key_nor_sdk():
    assert _aa(has_key=False, sdk_found=False, jev_mode="fake") == []


def test_auto_apply_warnings_missing_sdk_says_pip_install():
    out = _aa(sdk_found=False)
    assert len(out) == 1
    assert "pip install typesafe-sdk" in out[0]


def test_auto_apply_warnings_missing_playwright_says_pip_and_install_chromium():
    out = _aa(playwright_found=False, chromium_found=False)
    assert len(out) == 1, "no separate Chromium row when playwright itself is missing"
    assert "pip install playwright" in out[0]
    assert "playwright install chromium" in out[0]


def test_auto_apply_warnings_missing_chromium_alone():
    out = _aa(chromium_found=False)
    assert len(out) == 1
    assert "playwright install chromium" in out[0]
    assert "pip install playwright" not in out[0]


def test_auto_apply_warnings_everything_missing_is_three_rows():
    assert len(_aa(has_key=False, sdk_found=False, playwright_found=False,
                   chromium_found=False)) == 3


# --- chromium_installed: playwright importable AND a chromium-* build dir ---------

def test_chromium_installed_needs_playwright_and_a_build_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_check, "module_found", lambda name: name == "playwright")
    cache = tmp_path / "ms-playwright"
    assert setup_check.chromium_installed(cache_dirs=[cache]) is False
    cache.mkdir()
    assert setup_check.chromium_installed(cache_dirs=[cache]) is False, "empty cache, no build"
    (cache / "chromium-1181").mkdir()
    assert setup_check.chromium_installed(cache_dirs=[cache]) is True


def test_chromium_installed_false_when_playwright_is_not_importable(monkeypatch, tmp_path):
    cache = tmp_path / "ms-playwright"
    (cache / "chromium-1181").mkdir(parents=True)
    monkeypatch.setattr(setup_check, "module_found", lambda name: False)
    assert setup_check.chromium_installed(cache_dirs=[cache]) is False


def test_chromium_installed_ignores_a_file_that_only_starts_with_chromium(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_check, "module_found", lambda name: True)
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    (cache / "chromium-1181.txt").write_text("not a build dir", encoding="utf-8")
    assert setup_check.chromium_installed(cache_dirs=[cache]) is False


class _BoomDir:
    """A cache-dir stand-in whose listing always raises, like a dir that
    vanishes or a permission error mid-scan."""

    def is_dir(self):
        return True

    def iterdir(self):
        raise OSError("permission denied")


def test_chromium_installed_swallows_an_unreadable_cache_dir(monkeypatch):
    monkeypatch.setattr(setup_check, "module_found", lambda name: True)
    assert setup_check.chromium_installed(cache_dirs=[_BoomDir()]) is False


def test_chromium_cache_dirs_localappdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(setup_check.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert setup_check.chromium_cache_dirs() == [tmp_path / "lad" / "ms-playwright"]


def test_chromium_cache_dirs_empty_when_localappdata_is_blank_on_windows(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(setup_check.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert setup_check.chromium_cache_dirs() == []


def test_chromium_cache_dirs_mac_caches(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(setup_check.sys, "platform", "darwin")
    assert setup_check.chromium_cache_dirs() == [
        Path.home() / "Library" / "Caches" / "ms-playwright"]


def test_chromium_cache_dirs_linux_cache(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(setup_check.sys, "platform", "linux")
    assert setup_check.chromium_cache_dirs() == [Path.home() / ".cache" / "ms-playwright"]


def test_chromium_cache_dirs_honours_an_explicit_browsers_path(monkeypatch, tmp_path):
    custom = tmp_path / "custom-browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(custom))
    assert setup_check.chromium_cache_dirs() == [custom]


def test_chromium_cache_dirs_zero_resolves_the_playwright_package_dir(monkeypatch, tmp_path):
    pkg_dir = tmp_path / "site-packages" / "playwright"
    pkg_dir.mkdir(parents=True)

    class _Spec:
        submodule_search_locations = [str(pkg_dir)]

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    monkeypatch.setattr(setup_check.importlib.util, "find_spec", lambda name: _Spec())
    assert setup_check.chromium_cache_dirs() == [
        pkg_dir / "driver" / "package" / ".local-browsers"]


def test_chromium_cache_dirs_zero_is_empty_when_playwright_is_not_found(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    monkeypatch.setattr(setup_check.importlib.util, "find_spec", lambda name: None)
    assert setup_check.chromium_cache_dirs() == []


# --- auto_apply_problems: wiring, labels and best-effort silence -----------------

def _stub_auto_apply(monkeypatch, *, stored=None, secrets=None, env_key=None,
                     found=(), chromium=True):
    monkeypatch.setattr(setup_check.settings, "load", lambda: stored or {})
    monkeypatch.setattr(setup_check.settings, "secret_status", lambda: secrets or {})
    monkeypatch.setattr(setup_check, "module_found", lambda name: name in found)
    monkeypatch.setattr(setup_check, "chromium_installed", lambda **k: chromium)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    if env_key:
        monkeypatch.setenv("TYPESAFE_API_KEY", env_key)


def test_auto_apply_problems_clean_when_everything_is_in_place(monkeypatch):
    _stub_auto_apply(monkeypatch, secrets={"TYPESAFE_API_KEY": True},
                     found=("typesafe_sdk", "playwright"))
    assert setup_check.auto_apply_problems() == []


def test_auto_apply_problems_labels_every_row(monkeypatch):
    _stub_auto_apply(monkeypatch, found=(), chromium=False)
    out = setup_check.auto_apply_problems()
    assert len(out) == 3
    assert all(w.startswith("[Auto-apply] ") for w in out)


def test_auto_apply_problems_env_key_counts_as_present(monkeypatch):
    _stub_auto_apply(monkeypatch, env_key="ts-key", found=("typesafe_sdk", "playwright"))
    assert setup_check.auto_apply_problems() == []


def test_auto_apply_problems_honours_fake_mode_from_settings(monkeypatch):
    _stub_auto_apply(monkeypatch, stored={"auto_apply_jev_mode": "fake"},
                     found=("playwright",))
    assert setup_check.auto_apply_problems() == []


def test_auto_apply_problems_is_silent_when_settings_cannot_be_read(monkeypatch):
    def boom():
        raise OSError("config.json is locked")
    monkeypatch.setattr(setup_check.settings, "load", boom)
    assert setup_check.auto_apply_problems() == []


def test_local_problems_includes_the_auto_apply_rows(monkeypatch):
    from resume_tailor import master_validate
    monkeypatch.setattr(master_validate, "check_setup", lambda: {"master": [], "answers": []})
    monkeypatch.setattr(setup_check, "engine_problems", lambda: [])
    monkeypatch.setattr(setup_check, "auto_apply_problems", lambda: ["[Auto-apply] x"])
    assert setup_check.local_problems() == ["[Auto-apply] x"]


def test_module_found_uses_find_spec_without_importing():
    assert setup_check.module_found("json") is True
    assert setup_check.module_found("no_such_module_zzz") is False
    assert "no_such_module_zzz" not in sys.modules
