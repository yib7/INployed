"""Tests for the atomic JSON writer shared by the dashboard and watcher."""
import json
import os
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


def test_atomic_write_json_retries_replace_past_transient_lock(tmp_path, monkeypatch):
    # SP2 review: CPython's open() on Windows doesn't grant FILE_SHARE_DELETE,
    # so os.replace can transiently fail with PermissionError while a lock-free
    # reader holds the destination open. A short retry must absorb that.
    p = tmp_path / "config.json"
    p.write_text('{"old": true}', encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("reader holds the destination open")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(jsonutil.os, "replace", flaky)
    monkeypatch.setattr(jsonutil, "_REPLACE_RETRY", 0)   # don't sleep in tests
    atomic_write_json(p, {"new": 2})
    assert calls["n"] == 3
    assert json.loads(p.read_text(encoding="utf-8")) == {"new": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_json_reraises_after_retries_exhausted(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text('{"old": true}', encoding="utf-8")
    calls = {"n": 0}

    def always(src, dst, *a, **kw):
        calls["n"] += 1
        raise PermissionError("never lets go")

    monkeypatch.setattr(jsonutil.os, "replace", always)
    monkeypatch.setattr(jsonutil, "_REPLACE_RETRY", 0)
    with pytest.raises(OSError):
        atomic_write_json(p, {"new": 2})
    assert calls["n"] == jsonutil._REPLACE_TRIES         # bounded, then re-raise
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []            # tmp still cleaned


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
