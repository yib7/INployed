"""Shared pytest fixtures.

`master_tmp` / `master_tmp_broken` write a synthetic master_experience.yaml to a
temp dir and point `config.MASTER_YAML` at it (the real file is gitignored
personal data), so the résumé-data editor tests never touch the user's file.

`_drain_qt_widgets` (autouse) destroys widgets after every test so the single
shared QApplication never accumulates leaked ones — see the fixture for why that
matters (it's the difference between a 4-minute suite and a CI hang).
"""
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# Qt GUI tests run headless (CI has no display). Must be set before the first
# QApplication is created anywhere in the session.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Isolate the whole suite from the user's REAL %LOCALAPPDATA%\linkedin_watcher\.
#
# seen_db.SeenRegistry() (the application tracker + generated-résumé index),
# watcher.py (LOG_PATH/STATE_PATH, bound at *import* time — line 43 even mkdir's
# the dir) and the apply-queue / ats-accounts stores all derive their on-disk
# location from LOCALAPPDATA. A test that constructs any of them without an
# explicit tmp path reads AND writes the user's live files. That happened: pytest
# runs (some from worktrees) opened the real seen.db concurrently with the running
# dashboard + scheduled watcher and corrupted it twice (2026-06-28, 2026-07-07);
# app_status — the one table with no self-heal — was wiped both times. Redirect
# LOCALAPPDATA to a throwaway dir for the whole session, BEFORE any `local/` module
# is imported, so no test can ever touch the real profile. Subprocesses spawned by
# tests inherit this env, closing the subprocess-monkeypatch pollution hole too.
# Individual tests may still monkeypatch their own LOCALAPPDATA on top.
# tests/test_hermetic_appdata.py fails loudly if this redirect is ever removed.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="inployed-test-appdata-")

# Isolate the suite from the developer's REAL scrape_data/.env.
#
# Four modules call load_dotenv(<repo>/.env) at IMPORT time — local/app.py,
# local/resume_tailor/config.py, score_jobs.py and scraper.py — so merely
# importing any of them injects every var from that file into os.environ for the
# rest of the session. Measured on the author's machine: 22 vars, including
# GEMINI_API_KEYS and BRIGHT_DATA_API_TOKEN (a test asserting "no key configured"
# then silently exercises the WITH-key branch), and RESUME_TAILOR_OUTPUT — which
# points config.OUTPUT_ROOT at the user's real ~/Downloads/Generated_Resumes,
# where output.resolve_dir happily mkdir's into it. That is the same class of bug
# as the LOCALAPPDATA one above: a test that passes only on a machine whose .env
# happens to be shaped right, and that writes to real user data on the way.
#
# Neutralise load_dotenv process-wide BEFORE `local/` is importable, and scrub any
# var that a .env could have set so the modules see documented defaults. A test
# that wants a value sets it with monkeypatch.setenv, which is undone at teardown.
# tests/test_hermetic_dotenv.py fails loudly if this is ever removed.
try:
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.main.load_dotenv = lambda *a, **k: False
except (ImportError, AttributeError):    # python-dotenv absent: nothing to neutralise
    pass

for _leaked in (
    "GEMINI_API_KEYS", "GEMINI_API_KEY", "BRIGHT_DATA_API_TOKEN", "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HEALTHCHECK_URL",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
    "RESUME_TAILOR_OUTPUT", "RESUME_TAILOR_CANDIDATE", "RESUME_TAILOR_GEMINI_AUTH",
    "RESUME_TAILOR_PROVIDER", "LINKEDIN_EXTRA_MASTER", "APPLY_QUEUE_PATH",
):
    os.environ.pop(_leaked, None)

# Whatever a stray default resolves to, it must not be the real Downloads folder.
os.environ["RESUME_TAILOR_OUTPUT"] = tempfile.mkdtemp(prefix="inployed-test-output-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
# pipeline/ holds the flat pipeline modules (scraper, score_jobs, keypool, …),
# imported by bare name so the same files run standalone on the VM.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

_MASTER_YAML = textwrap.dedent("""\
    # top comment
    basics:
      name: Jane Doe
      email: jane@example.com
    experience:
      - org: Example Corp
        title: Intern
        dates: "2024-06 / 2024-08"
        achievements:
          - id: a1
            what: did a thing
            angles: [backend]
    projects:
      - name: ProjX
        dates: "2024"
        achievements:
          - id: p1
            what: built x
            angles: [llm]
""")

_MASTER_YAML_BROKEN = textwrap.dedent("""\
    # broken: no basics, duplicate atom id across sections
    experience:
      - org: X
        dates: "2024"
        achievements:
          - id: dup
            what: w
            angles: [a]
    projects:
      - name: Y
        dates: "2024"
        achievements:
          - id: dup
            what: w
            angles: [b]
""")


def _master_fixture(tmp_path, monkeypatch, text):
    from resume_tailor import assets, config
    p = tmp_path / "master_experience.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    cached = (
        assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks,
        assets.skill_aliases, assets.skill_aliases_match_only,
    )
    for fn in cached:
        fn.cache_clear()
    return p, cached


@pytest.fixture
def master_tmp(tmp_path, monkeypatch):
    p, cached = _master_fixture(tmp_path, monkeypatch, _MASTER_YAML)
    yield p
    for fn in cached:
        fn.cache_clear()


@pytest.fixture
def master_tmp_broken(tmp_path, monkeypatch):
    p, cached = _master_fixture(tmp_path, monkeypatch, _MASTER_YAML_BROKEN)
    yield p
    for fn in cached:
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _restore_environ():
    """Undo any os.environ change a test leaves behind.

    monkeypatch.setenv is restored automatically, but APPLICATION code writes the
    environment directly — MainWindow._apply_auth_env does
    `os.environ["RESUME_TAILOR_GEMINI_AUTH"] = ...` so the in-process tailor picks
    the mode up at call time. Any Qt test that reaches a tailor or manual-add path
    therefore leaks that var into the rest of the session, and
    `config.gemini_auth()` reads the env var ahead of config.json — so
    test_llm_backend / test_llm_timeout passed or failed depending on whether a
    Qt test happened to run first. Found by running the suite in reverse file
    order; two tests failed that way and pass alphabetically.

    Declared FIRST among the autouse fixtures on purpose: setup runs first, so
    teardown runs LAST, after every other fixture has undone its own patches.
    That makes this the final word on the environment for each test.
    """
    snapshot = dict(os.environ)
    yield
    if os.environ != snapshot:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _hermetic_apply_queue(tmp_path):
    """SP3: MainWindow now mounts an ApplyQueuePanel that reads (and watches)
    the apply-queue file and probes the master-password state on construction.
    Point every test at a scratch queue and stub the panel's password seam so
    no test ever touches the real %LOCALAPPDATA% queue or the Windows
    Credential Manager. Tests that care set their own env/seam on top (their
    monkeypatch runs later, so it wins).

    Uses a PRIVATE MonkeyPatch instance, NOT the `monkeypatch` fixture: an
    autouse conftest fixture requesting `monkeypatch` would instantiate the
    shared instance first and so tear it down LAST — after module-level autouse
    fixtures — breaking any module fixture that expects the test's patches to
    be undone by its own teardown (e.g. test_active_verbs' cache_clear)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APPLY_QUEUE_PATH", str(tmp_path / "apply_queue.json"))
        panel_mod = sys.modules.get("qt.apply_queue_panel")
        if panel_mod is not None:
            mp.setattr(panel_mod, "_default_password_exists", lambda: False)
        yield


@pytest.fixture(autouse=True)
def _hermetic_outbox_and_vm(tmp_path):
    """No test may touch the real <repo>/outbox/ or spawn gcloud.

    Any code path reaching outbox's module defaults (OUTBOX_DIR / RUN_STATS_CSV /
    MASTER_CSV) or vm_sync.run_cmd from a test is a leak: between 2026-07-04 and
    07-08 every full-suite run queued a REAL outbox/local_stats_*.csv (69 piled
    up) and then 'pushed' them through a module-global subprocess fake. Redirect
    the defaults into tmp and stub run_cmd with a fast deterministic failure.
    Tests that need push mechanics inject runner=/their own monkeypatch (applied
    later, so it wins). Same private-MonkeyPatch pattern as _hermetic_apply_queue."""
    import subprocess as _subprocess

    import outbox
    import vm_sync
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(outbox, "OUTBOX_DIR", tmp_path / "hermetic_outbox")
        mp.setattr(outbox, "RUN_STATS_CSV", tmp_path / "hermetic_run_stats.csv")
        mp.setattr(outbox, "MASTER_CSV", tmp_path / "hermetic_master.csv")
        blocked = _subprocess.CompletedProcess(
            args=["blocked"], returncode=97, stdout="",
            stderr="vm_sync.run_cmd blocked by conftest (hermetic tests)")
        mp.setattr(vm_sync, "run_cmd", lambda cmd: blocked)
        yield


@pytest.fixture(autouse=True)
def _drain_qt_widgets():
    """Destroy widgets after each test so the shared QApplication doesn't leak them.

    pytest-qt closes widgets but their actual destruction is deferred (deleteLater),
    and nothing drains that queue between tests. Across the hundreds of Qt tests in
    this suite the closed-but-alive widgets pile up — and theme.apply_theme /
    set_scale iterate `app.allWidgets()` while setStyleSheet re-polishes *every*
    widget, so those whole-application operations grow O(accumulated) until a test
    effectively hangs. It's timing-dependent (the victim test shifts run to run),
    which is exactly how it slipped through locally yet hung CI for hours. Closing
    top-level widgets and flushing the DeferredDelete queue after every test keeps
    `allWidgets()` bounded, making the suite both fast and deterministic. A no-op for
    the non-Qt tests (no QApplication exists)."""
    yield
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError:
        return
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
