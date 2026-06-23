"""Shared pytest fixtures.

`root` is a single, **session-scoped** Tk interpreter shared by every GUI test.
Creating more than one Tk() per process is flaky on Windows (intermittent
TclError on the 2nd+ root), so all GUI tests reuse this one. It's withdrawn
(never shown) and destroyed at the end of the session.

`master_tmp` / `master_tmp_broken` write a synthetic master_experience.yaml to a
temp dir and point `config.MASTER_YAML` at it (the real file is gitignored
personal data), so the résumé-data editor tests never touch the user's file.
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

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - tkinter missing
    tk = None

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


@pytest.fixture(scope="session")
def root():
    if tk is None:
        pytest.skip("tkinter not available")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
