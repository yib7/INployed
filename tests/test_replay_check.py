"""`scripts/replay_check.py`: the release gate that replays the runner tests.

Only the pure parts run here (the child environment, the command, the verdict);
the replay itself is the script's job and takes minutes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("replay_check", REPO / "scripts" / "replay_check.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def test_the_child_replays_and_can_never_record(tmp_path):
    base = {"PATH": "p", "AUTO_APPLY_TEST_JEV": "record", "AUTO_APPLY_CAPTURE_JEV": "record",
            "AUTO_APPLY_JEV_PRUNE": "1", "AUTO_APPLY_RECORD_DRY": "1",
            "AUTO_APPLY_RECORD_USD_CAP": "5", "TYPESAFE_API_KEY": "synthetic-not-a-key",
            "AUTO_APPLY_JEV_CACHE": "elsewhere.json", "PYTEST_ADDOPTS": "-n 4"}
    env = rc.replay_env(base, tmp_path / "cache.json")
    assert env["AUTO_APPLY_TEST_JEV"] == "replay"
    assert env["AUTO_APPLY_JEV_CACHE"] == str(tmp_path / "cache.json")
    for gone in ("AUTO_APPLY_CAPTURE_JEV", "AUTO_APPLY_JEV_PRUNE", "AUTO_APPLY_RECORD_DRY",
                 "AUTO_APPLY_RECORD_USD_CAP", "TYPESAFE_API_KEY", "PYTEST_ADDOPTS"):
        assert gone not in env
    assert env["PATH"] == "p"


def test_the_command_is_the_serial_runner_set():
    cmd = rc.pytest_command(["-x"])
    assert cmd[:3] == [sys.executable, "-m", "pytest"]
    for f in rc.jev_harness.RUNNER_TESTS.split():
        assert f in cmd
    assert "-x" in cmd and not any(a.startswith("-n") for a in cmd)


@pytest.mark.parametrize("extra", [["-n", "4"], ["-n4"], ["--numprocesses=2"]])
def test_xdist_is_refused(extra):
    with pytest.raises(SystemExit, match="serially"):
        rc.pytest_command(extra)


def _rec(test, **kw):
    return {"test": test, "misses": [], "divergence": None, "result": "passed", **kw}


def test_a_clean_run_passes():
    clean, lines = rc.verdict([_rec("t::a"), _rec("t::b")], 0)
    assert clean and lines[-1] == "replay check: PASS"


@pytest.mark.parametrize("records,code,why", [
    ([_rec("t::a", misses=[{"questions": ["q2", "q1"]}], result="failed")], 1, "MISS t::a"),
    ([_rec("t::a", divergence="assert 1 == 2", result="xfail")], 0, "DIVERGED t::a"),
    ([_rec("t::a", result="failed")], 1, "FAILED t::a"),
    ([_rec("t::a")], 1, "pytest exit 1"),
    ([], 0, "no test used the judge"),
])
def test_a_miss_a_divergence_or_a_failure_fails(records, code, why):
    clean, lines = rc.verdict(records, code)
    assert not clean and lines[-1] == "replay check: FAIL"
    assert any(why in x for x in lines), lines


def test_a_miss_names_its_questions_and_the_recording_command():
    _, lines = rc.verdict([_rec("t::a", misses=[{"questions": ["q2", "q1"]}],
                                result="failed")], 1)
    text = "\n".join(lines)
    assert "['q1', 'q2']" in text and "jev_record.ps1" in text


def test_a_cache_the_run_changed_fails():
    clean, lines = rc.verdict([_rec("t::a")], 0, cache_changed=True)
    assert not clean and any("cache copy changed" in x for x in lines)
