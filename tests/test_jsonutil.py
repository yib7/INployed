"""Tests for the atomic JSON writer shared by the dashboard and watcher."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

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
