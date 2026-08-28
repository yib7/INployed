"""Guard: the suite must never read the developer's REAL repo-root files.

`local/config.json`, `search_config.json`, `scoring_config.json` and
`apply_answers.json` are git-ignored personal files, and `linkedin_jobs_master.csv`
is a 37 MB personal dataset. A test that reads one of them takes a different branch
on the author's machine than it does on CI or a fresh clone, which is the
"passes only on your machine" failure the whole conftest sandbox exists to prevent.

It was not hypothetical. Measured over the full suite on 2026-08-27 with a
`Path.read_text` probe: 316 tests read one of those four config files straight out
of the working tree. The author's `search_config.json` carries
`limit_per_input=150` and `exclude_window_days=14` while `scraper.py`'s built-in
defaults are 100 and 90, so every scraper test reaching `load_search_config()` was
asserting against numbers a fresh clone does not have.

conftest's `_hermetic_repo_data` fixture redirects every one of those paths into a
throwaway dir. These tests fail loudly if that redirect is ever removed.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _is_sandboxed(p) -> bool:
    """True when `p` lives outside the repo working tree."""
    try:
        Path(p).resolve().relative_to(REPO)
    except ValueError:
        return True
    return False


def test_scraper_data_paths_are_sandboxed():
    import scraper
    for attr in ("OUTPUT_DIR", "MASTER_CSV", "PREVIOUS_IDS_FILE",
                 "EXTERNAL_EXCLUDE_FILE", "BLOCKLIST_FILE"):
        p = getattr(scraper, attr)
        assert _is_sandboxed(p), f"scraper.{attr} still points into the repo: {p}"


def test_score_jobs_data_paths_are_sandboxed():
    import score_jobs
    for attr in ("OUTPUT_DIR", "MASTER_CSV", "RESUME_PATH", "RUN_STATS_CSV"):
        p = getattr(score_jobs, attr)
        assert _is_sandboxed(p), f"score_jobs.{attr} still points into the repo: {p}"


def test_search_config_reads_the_builtin_defaults_not_the_authors_file():
    """The concrete symptom, pinned: with the redirect in place these come back as
    the module constants, whatever the developer has configured locally."""
    import scraper
    cfg = scraper.load_search_config()
    assert cfg["limit_per_input"] == scraper.LIMIT_PER_INPUT
    assert cfg["exclude_window_days"] == scraper.DEFAULT_EXCLUDE_WINDOW_DAYS
    assert cfg["keywords"] == scraper.KEYWORDS


def test_dashboard_config_paths_are_sandboxed():
    import jobsdata
    import settings
    import watcher
    assert _is_sandboxed(jobsdata._cfg_path()), jobsdata._cfg_path()
    assert _is_sandboxed(watcher.CONFIG_PATH), watcher.CONFIG_PATH
    for tid, path in settings._resolve_targets(None).items():
        assert _is_sandboxed(path), f"settings target {tid!r} is not sandboxed: {path}"


def test_apply_answer_store_is_sandboxed():
    from resume_tailor import apply_answers, apply_config
    assert _is_sandboxed(apply_answers.STORE_PATH), apply_answers.STORE_PATH
    assert _is_sandboxed(apply_config.APPLY_CONFIG), apply_config.APPLY_CONFIG


def test_target_files_itself_is_left_truthful():
    """The redirect goes on the READER, not on the map: `TARGET_FILES` is
    production config that test_settings asserts against, so it keeps pointing at
    the real paths even while nothing reads through it."""
    import settings
    assert not _is_sandboxed(settings.TARGET_FILES["config"])
    assert settings.TARGET_FILES["search"].name == "search_config.json"


def test_the_pipeline_dotenv_optout_is_armed_for_the_whole_suite():
    """Belt to the load_dotenv monkeypatch: both billed scripts read this at
    import, so the suite stays disarmed even if the patch stops biting."""
    import os
    assert os.environ.get("INPLOYED_NO_DOTENV", "").strip().lower() in (
        "1", "true", "yes", "on")
