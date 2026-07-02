"""Tests for the atomic JSON writer shared by the dashboard and watcher."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jsonutil  # noqa: E402
from jsonutil import atomic_write_json  # noqa: E402


def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    atomic_write_json(p, {"a": 1, "b": "x"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "x"}


def test_atomic_write_json_overwrites_existing(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(p, {"new": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"new": 2}


def test_atomic_write_json_leaves_no_temp_file(tmp_path):
    p = tmp_path / "config.json"
    atomic_write_json(p, {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_json_cleans_up_tmp_when_replace_fails(tmp_path, monkeypatch):
    # BONUS (SP3 review): if os.replace raises (e.g. Windows destination locked),
    # the tmp file must not be stranded, and the pre-existing target must be
    # left exactly as it was -- the write is all-or-nothing.
    p = tmp_path / "config.json"
    p.write_text('{"old": true}', encoding="utf-8")
    before = p.read_bytes()

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(jsonutil.os, "replace", boom_replace)

    with pytest.raises(OSError):
        atomic_write_json(p, {"new": 2})

    assert p.read_bytes() == before             # untouched: os.replace never landed
    assert list(tmp_path.glob("*.tmp")) == []    # no stray tmp file left behind
