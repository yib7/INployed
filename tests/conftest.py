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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

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
