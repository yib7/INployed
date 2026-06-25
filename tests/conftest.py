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
import textwrap
from pathlib import Path

import pytest

# Qt GUI tests run headless (CI has no display). Must be set before the first
# QApplication is created anywhere in the session.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
    cached = (assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks)
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
