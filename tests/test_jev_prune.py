"""The SP8 replay-cache prune: `jev.ReplayJev.used_keys` and `jev.prune_cache`
(covered in `test_jev.py`), and the harness wiring on top of them --
`jev_harness.Session.prune`, `write_used_keys`, `prune_if_asked`, and
`conftest_jev`'s `pytest_sessionfinish` hook that calls them.

Hermetic: no key, no network, every cache lives under `tmp_path`. The hook
tests run an inner pytest through `pytester` in-process, the same way
`test_jev_harness.py`'s fixture tests do, with `conftest_jev` loaded exactly
as a runner test module loads it."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
import jev_harness  # noqa: E402

pytest_plugins = ["pytester", "conftest_jev"]

STATE = {"page": {"title": "Apply for the analytics engineer role"},
         "fields": [{"n": 0, "label": "Email"}]}
QUESTIONS = {
    "page_state": {"type": "choice", "instructions": "What kind of page is `page`?",
                   "criteria": {"application_form": "a form to apply for a role",
                                "other": "anything else"}},
}


@pytest.fixture(autouse=True)
def _clean_usage():
    jev.reset_usage()
    yield
    jev.reset_usage()


def _seed(path: Path, keys: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8")


# --- Session.prune: valid only with replay mode --------------------------------------

def test_session_refuses_prune_outside_replay_mode(tmp_path):
    with pytest.raises(ValueError, match=jev_harness.PRUNE_ENV):
        jev_harness.Session("record", tmp_path / "cache.json", 0.5, prune=True)
    with pytest.raises(ValueError, match=jev_harness.PRUNE_ENV):
        jev_harness.Session("fake", tmp_path / "cache.json", prune=True)


def test_session_from_env_reads_the_prune_flag_and_rejects_it_outside_replay(tmp_path):
    cache = tmp_path / "cache.json"
    s = jev_harness.Session.from_env({jev_harness.MODE_ENV: "replay",
                                      jev.CACHE_ENV: str(cache),
                                      jev_harness.PRUNE_ENV: "1"})
    assert s.prune is True
    assert jev_harness.Session.from_env({jev_harness.MODE_ENV: "replay",
                                         jev.CACHE_ENV: str(cache)}).prune is False
    with pytest.raises(ValueError, match=jev_harness.PRUNE_ENV):
        jev_harness.Session.from_env({jev_harness.MODE_ENV: "record",
                                      jev_harness.CAP_ENV: "0.1",
                                      jev_harness.PRUNE_ENV: "true"})


def test_prune_from_reads_1_true_or_yes():
    assert jev_harness.prune_from({}) is False
    for raw in ("1", "true", "True", "yes", "YES"):
        assert jev_harness.prune_from({jev_harness.PRUNE_ENV: raw}) is True
    assert jev_harness.prune_from({jev_harness.PRUNE_ENV: "0"}) is False


# --- write_used_keys -------------------------------------------------------------------

def test_write_used_keys_writes_the_sorted_keys_beside_outcomes(tmp_path):
    cache = tmp_path / "cache.json"
    s = jev_harness.Session("replay", cache)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"b", "a"}
    path = jev_harness.write_used_keys(s)
    assert path == jev_harness.outcomes_path(cache).with_name("used_keys.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"mode": "replay", "used_keys": ["a", "b"]}


def test_write_used_keys_with_no_replay_yet_writes_an_empty_list(tmp_path):
    cache = tmp_path / "cache.json"
    s = jev_harness.Session("replay", cache)
    path = jev_harness.write_used_keys(s)
    assert json.loads(path.read_text(encoding="utf-8")) == {"mode": "replay", "used_keys": []}


# --- prune_if_asked ----------------------------------------------------------------------

def test_prune_if_asked_does_nothing_when_prune_was_not_set(tmp_path):
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache)
    assert jev_harness.prune_if_asked(s, 0) == ""
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_if_asked_prunes_a_clean_run(tmp_path):
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    line = jev_harness.prune_if_asked(s, 0)
    assert "kept 1 of 2" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used"}


def test_prune_if_asked_refuses_after_a_replay_miss(tmp_path):
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    s.replay.misses = 1
    line = jev_harness.prune_if_asked(s, 0)
    assert line.startswith("jev prune refused:") and "miss" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_if_asked_refuses_after_a_test_failure(tmp_path):
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    line = jev_harness.prune_if_asked(s, 1)         # testsfailed=1, no miss
    assert line.startswith("jev prune refused:") and "failure" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_if_asked_with_no_replay_refuses_with_no_keys(tmp_path):
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    line = jev_harness.prune_if_asked(s, 0)
    assert line.startswith("jev prune refused:") and "no keys" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used"}


# --- the fixture and hooks, through an inner pytest ---------------------------------------

_INNER = '''
import jev, jev_harness
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"
'''


def _inner(pytester):
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER.format(STATE=STATE, QUESTIONS=QUESTIONS))


def test_a_clean_replay_prunes_stale_keys_from_the_cache(pytester, monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    used_key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    _seed(cache, {used_key: {"page_state": {"kind": "choice", "choice": "application_form",
                                            "probabilities": {"application_form": 1.0,
                                                              "other": 0.0},
                                            "confidence": 1.0}},
                 "stale-key": {"page_state": {"kind": "choice", "choice": "other",
                                              "probabilities": {}, "confidence": 1.0}}})
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*jev prune: kept 1 of 2 key(s)*"])
    kept = json.loads(cache.read_text(encoding="utf-8"))
    assert set(kept) == {used_key}
    used = json.loads(jev_harness.used_keys_path(cache).read_text(encoding="utf-8"))
    assert used == {"mode": "replay", "used_keys": [used_key]}


def test_a_replay_miss_refuses_to_prune(pytester, monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"       # no cache file at all: the one request misses
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*miss*"])
    assert not cache.exists()


def test_prune_never_fires_without_the_env_var(pytester, monkeypatch, tmp_path):
    # rule check: replay alone, with no AUTO_APPLY_JEV_PRUNE, must not prune
    cache = tmp_path / "cache.json"
    used_key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    _seed(cache, {used_key: {"page_state": {"kind": "choice", "choice": "application_form",
                                            "probabilities": {}, "confidence": 1.0}},
                 "stale-key": {"page_state": {"kind": "choice", "choice": "other",
                                              "probabilities": {}, "confidence": 1.0}}})
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.delenv(jev_harness.PRUNE_ENV, raising=False)
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "jev prune" not in result.stdout.str()
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {used_key, "stale-key"}
