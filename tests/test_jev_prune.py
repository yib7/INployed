"""The replay-cache prune: `jev.ReplayJev.used_keys` and `jev.prune_cache`
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


def test_prune_if_asked_refuses_on_a_narrowed_reason_before_touching_the_cache(tmp_path):
    # a caller-supplied narrowed_reason (a run that was not the
    # whole RUNNER_TESTS set) refuses immediately, even on an otherwise clean
    # replay with keys to spare: jev.prune_cache is never reached
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    line = jev_harness.prune_if_asked(s, 0, narrowed_reason="-k 'foo' narrows which tests ran")
    assert line == "jev prune refused: -k 'foo' narrows which tests ran"
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_if_asked_refuses_on_a_skip_reason_before_touching_the_cache(tmp_path):
    # a jev_judge test that skipped for its own reason (not
    # the spend cap, not an unrecorded miss) never asked for its keys, so
    # skip_reason refuses the same way narrowed_reason does, before
    # jev.prune_cache is ever reached
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    line = jev_harness.prune_if_asked(
        s, 0, skip_reason="1 jev_judge test(s) skipped for a reason other than a spend-cap "
                          "stop or an unrecorded flow, so its keys were never asked for: "
                          "test_inner.py::test_skipped")
    assert line.startswith("jev prune refused:") and "test_inner.py::test_skipped" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_if_asked_refuses_after_a_divergence(tmp_path):
    # a replay assertion turned into an xfail counts as no
    # failure, and the test stopped before its later requests: their keys
    # were never asked for
    cache = tmp_path / "cache.json"
    _seed(cache, {"used": {}, "stale": {}})
    s = jev_harness.Session("replay", cache, prune=True)
    s.replay = jev.ReplayJev(None, cache)
    s.replay.used_keys = {"used"}
    rec = s.begin("test_inner.py::test_one")
    s.end("test_inner.py::test_one")
    rec.divergence = "AssertionError: assert 'other' == 'application_form'"
    line = jev_harness.prune_if_asked(s, 0)
    assert line.startswith("jev prune refused:") and "test_inner.py::test_one" in line
    assert "diverged" in line
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


# --- jev_skip_reason: a jev_judge test's own skip, apart from the cap or an unrecorded miss -----

def test_jev_skip_reason_is_empty_with_nothing_skipped():
    assert jev_harness.jev_skip_reason([]) == ""


def test_jev_skip_reason_names_every_skipped_test():
    reason = jev_harness.jev_skip_reason([("test_inner.py::test_a", "Skipped: nope"),
                                          ("test_inner.py::test_b", "Skipped: also nope")])
    assert "2" in reason
    assert "test_inner.py::test_a" in reason and "test_inner.py::test_b" in reason


# --- runner_narrowed_reason: the whole RUNNER_TESTS set, no -k/-m/deselect/node id ------------

def test_runner_narrowed_reason_is_empty_for_a_clean_full_run(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py b.py")
    assert jev_harness.runner_narrowed_reason(
        collected_files={"a.py", "b.py"}, keyword="", markexpr="", deselected=0, args=()) == ""


def test_runner_narrowed_reason_names_a_missing_file(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py b.py")
    reason = jev_harness.runner_narrowed_reason(
        collected_files={"a.py"}, keyword="", markexpr="", deselected=0, args=())
    assert "did not run" in reason and "b.py" in reason


def test_runner_narrowed_reason_names_a_keyword_filter(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py")
    reason = jev_harness.runner_narrowed_reason(
        collected_files={"a.py"}, keyword="test_one", markexpr="", deselected=0, args=())
    assert "-k" in reason and "test_one" in reason


def test_runner_narrowed_reason_names_a_mark_filter(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py")
    reason = jev_harness.runner_narrowed_reason(
        collected_files={"a.py"}, keyword="", markexpr="slow", deselected=0, args=())
    assert "-m" in reason and "slow" in reason


def test_runner_narrowed_reason_names_a_deselected_count(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py")
    reason = jev_harness.runner_narrowed_reason(
        collected_files={"a.py"}, keyword="", markexpr="", deselected=3, args=())
    assert "3" in reason and "deselected" in reason


def test_runner_narrowed_reason_names_a_node_id(monkeypatch):
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "a.py")
    reason = jev_harness.runner_narrowed_reason(
        collected_files={"a.py"}, keyword="", markexpr="", deselected=0,
        args=("a.py::test_one",))
    assert "node id" in reason and "a.py::test_one" in reason


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
    # the whole RUNNER_TESTS set here is this one inner file (_inner's own
    # "test_inner.py"), so the full-run check passes
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
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
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*miss*"])
    assert not cache.exists()


_INNER_DIVERGES = '''
import jev, jev_harness
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
LATER = {LATER!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    judge = jev_judge()
    answers = judge.judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"
    judge.judge(LATER, QUESTIONS)
'''


def test_a_diverged_replay_refuses_to_prune(pytester, monkeypatch, tmp_path):
    # the test asks k1, its assertion fails (an xfail in
    # replay mode, no failure), and it never asks k2; the prune would drop k2
    later = {**STATE, "page": {"title": "Review your application"}}
    k1, k2 = jev.ReplayJev.key_for(STATE, QUESTIONS), jev.ReplayJev.key_for(later, QUESTIONS)
    cache = tmp_path / "cache.json"
    _seed(cache, {k1: {"page_state": {"kind": "choice", "choice": "other",
                                      "probabilities": {"application_form": 0.1, "other": 0.9},
                                      "confidence": 0.9}},
                  k2: {"page_state": {"kind": "choice", "choice": "other",
                                      "probabilities": {}, "confidence": 1.0}}})
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_DIVERGES.format(STATE=STATE, LATER=later,
                                                          QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(xfailed=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*diverged*test_inner.py::test_one*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {k1, k2}


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


# --- pruning refuses unless the whole RUNNER_TESTS set ran (no -k/-m, no ------------------------
# --- deselection, no missing file, no node id narrower than a file) -----------------------------

_INNER_A = '''
import jev, jev_harness
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"
'''

_INNER_B = '''
import jev, jev_harness
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"

def test_two():
    assert True
'''


def _two_files(pytester):
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner_a=_INNER_A.format(STATE=STATE, QUESTIONS=QUESTIONS),
                        test_inner_b=_INNER_B.format(STATE=STATE, QUESTIONS=QUESTIONS))


def _seed_used_and_stale(cache):
    used_key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    _seed(cache, {used_key: {"page_state": {"kind": "choice", "choice": "application_form",
                                            "probabilities": {"application_form": 1.0,
                                                              "other": 0.0},
                                            "confidence": 1.0}},
                 "stale-key": {"page_state": {"kind": "choice", "choice": "other",
                                              "probabilities": {}, "confidence": 1.0}}})
    return used_key


def _two_file_env(monkeypatch, cache):
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner_a.py test_inner_b.py")


def test_a_full_run_of_both_runner_files_prunes(pytester, monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    used_key = _seed_used_and_stale(cache)
    _two_file_env(monkeypatch, cache)
    _two_files(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)      # test_inner_a::test_one, _b::test_one, _b::test_two
    result.stdout.fnmatch_lines(["*jev prune: kept 1 of 2 key(s)*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {used_key}


def test_a_single_file_run_refuses_to_prune(pytester, monkeypatch, tmp_path):
    # only test_inner_a.py ran: RUNNER_TESTS also names test_inner_b.py, so
    # this run's used_keys never had a chance to cover it
    cache = tmp_path / "cache.json"
    _seed_used_and_stale(cache)
    _two_file_env(monkeypatch, cache)
    _two_files(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider", "test_inner_a.py")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*did not run*test_inner_b.py*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {
        jev.ReplayJev.key_for(STATE, QUESTIONS), "stale-key"}


def test_a_keyword_filtered_run_refuses_to_prune(pytester, monkeypatch, tmp_path):
    # -k test_one still collects both files but deselects test_inner_b::test_two
    cache = tmp_path / "cache.json"
    _seed_used_and_stale(cache)
    _two_file_env(monkeypatch, cache)
    _two_files(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider", "-k", "test_one")
    result.assert_outcomes(passed=2, deselected=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*-k*test_one*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {
        jev.ReplayJev.key_for(STATE, QUESTIONS), "stale-key"}


_INNER_UNRECORDED_PRUNE = '''
import pytest
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

@pytest.mark.jev_unrecorded
def test_marked(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)
'''


def test_an_unrecorded_skip_blocks_the_prune(pytester, monkeypatch, tmp_path):
    # a jev_unrecorded miss turns the test's own report into a
    # skip (conftest_jev.pytest_runtest_makereport), never a failure, so
    # testsfailed stays 0. The miss itself must still block the prune
    # (jev.prune_cache's own misses gate)
    cache = tmp_path / "cache.json"       # no cache file: the one request misses
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_UNRECORDED_PRUNE.format(STATE=STATE,
                                                                  QUESTIONS=QUESTIONS))
    # test_inner.py's own `pytest_plugins = ["conftest_jev"]` loads the
    # plugin too late to register the `jev_unrecorded` marker before this
    # same module's `@pytest.mark.jev_unrecorded` line is evaluated at
    # import time (a module's own `pytest_plugins` list is only considered
    # once the module has fully imported, after its decorators ran): an
    # order-dependent `PytestUnknownMarkWarning` that this test's own
    # `-W error::...` would promote to a collection error, masked only when
    # an earlier test in the same process registers the same mark first.
    # `-p conftest_jev` forces the plugin in before collection starts, the
    # same way test_jev_harness.py's two `_INNER_UNRECORDED` tests do.
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider",
                                          "-p", "conftest_jev",
                                          "-W", "error::pytest.PytestUnknownMarkWarning")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*miss*"])


# --- a jev_judge test's own skip, apart from a spend-cap stop or an -----------------------------
# --- unrecorded miss, must also block the prune (it never got to ask for its keys) --------------

_INNER_EXTERNAL_SKIP = '''
import pytest
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_kept(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"

def test_skipped_for_its_own_reason(jev_judge):
    pytest.skip("unrelated to jev: a fixture this run does not have")
'''


def test_a_jev_judge_tests_own_skip_blocks_the_prune(pytester, monkeypatch, tmp_path):
    # an unsafe prune: a test that requests
    # jev_judge but skips for a reason of its own (a platform skipif, an
    # importorskip, a pytest.skip() in the body, as here) never calls the
    # judge, so it leaves no replay miss for jev.prune_cache's own gate to
    # catch. conftest_jev must tally the skip itself and refuse on it.
    cache = tmp_path / "cache.json"
    used_key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    _seed(cache, {used_key: {"page_state": {"kind": "choice", "choice": "application_form",
                                            "probabilities": {}, "confidence": 1.0}},
                 "stale-key": {"page_state": {"kind": "choice", "choice": "other",
                                              "probabilities": {}, "confidence": 1.0}}})
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_EXTERNAL_SKIP.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*jev prune refused:*test_skipped_for_its_own_reason*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {used_key, "stale-key"}


_INNER_UNRELATED_SKIP = '''
import pytest
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_kept(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"

@pytest.mark.skip(reason="a permanent, unrelated skip: this test never touches jev_judge")
def test_unrelated_skip():
    assert False
'''


def test_an_unrelated_tests_skip_never_blocks_the_prune(pytester, monkeypatch, tmp_path):
    # a test that never requests jev_judge does not touch the cache either
    # way (a POSIX-only test skipped on Windows, with no jev_judge fixture,
    # does not block a clean prune)
    cache = tmp_path / "cache.json"
    used_key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    _seed(cache, {used_key: {"page_state": {"kind": "choice", "choice": "application_form",
                                            "probabilities": {}, "confidence": 1.0}},
                 "stale-key": {"page_state": {"kind": "choice", "choice": "other",
                                              "probabilities": {}, "confidence": 1.0}}})
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    monkeypatch.setenv(jev_harness.PRUNE_ENV, "1")
    monkeypatch.setattr(jev_harness, "RUNNER_TESTS", "test_inner.py")
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_UNRELATED_SKIP.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*jev prune: kept 1 of 2 key(s)*"])
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {used_key}
