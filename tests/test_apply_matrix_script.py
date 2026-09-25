"""`scripts/apply_matrix.py`'s `--jobs` option.

- The parity check: three small, route-free, local-only flows (`post_form`,
  `lever_single_park`, `ashby_wizard_park`; no LinkedIn, no inbox, no ATS
  account) with two noisy seeds, run through the real CLI as a subprocess
  once with `--jobs 1` (today's serial path) and once with `--jobs 3` (one
  worker process per flow). Both must write the same `--json` content, apart
  from the per-run `seconds` and `trace` fields a parallel run cannot pin
  down, and print the same `h.summary()` text apart from the run's own
  wall-clock line.
- `_next_message`'s own timeout arithmetic, against a fake queue and a fake
  process: it gives up at the deadline for a worker still running, and gives
  up promptly (not at the deadline) once the worker has died with nothing
  left to read.
- A flow whose name is not in the registry makes its worker raise
  (`apply_harness.flow` finds nothing): `_run_parallel` reports it as one
  "failed" row naming the cause, never a hang. This needs no browser, so it
  runs whether or not Chromium is installed.

The parity check needs real Playwright browsers (each subprocess launches
its own), so it shares the browser tests' skip: `pytest_plugins =
["conftest_browser"]`, and it requests the `_browser` fixture purely for
that skip -- `apply_matrix.main` never touches that fixture's own browser,
it opens its own (one per worker under `--jobs 3`, or one in-process under
`--jobs 1`)."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for sub in ("scripts", "local", "tests"):
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import apply_harness as h  # noqa: E402
import apply_matrix  # noqa: E402

pytest_plugins = ["conftest_browser"]

# three small, local-only, route-free flows: fast under FAST_TIMING, no
# LinkedIn, no inbox, no ATS account
_SMALL_FLOWS = ("post_form", "lever_single_park", "ashby_wizard_park")
_WALL_TIME = re.compile(r" in \d+s$", re.M)


def _run_script(json_path: Path, jobs: int) -> str:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "apply_matrix.py"),
         "--flows", ",".join(_SMALL_FLOWS), "--seeds", "2", "--jobs", str(jobs),
         "--json", str(json_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO), timeout=55)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_jobs_1_and_jobs_3_agree_on_three_small_flows(_browser, tmp_path):
    serial_json = tmp_path / "serial.json"
    parallel_json = tmp_path / "parallel.json"
    serial_out = _run_script(serial_json, 1)
    parallel_out = _run_script(parallel_json, 3)

    # the run's own wall-clock line is the one thing `--jobs` is meant to
    # change; everything else in the printed summary must match
    assert _WALL_TIME.sub(" in Ns", serial_out) == _WALL_TIME.sub(" in Ns", parallel_out)

    serial_rows = json.loads(serial_json.read_text(encoding="utf-8"))
    parallel_rows = json.loads(parallel_json.read_text(encoding="utf-8"))
    assert len(serial_rows) == len(_SMALL_FLOWS) * 3          # fake + 2 noisy seeds

    volatile = {"seconds", "trace"}       # a run's own timing and temp-dir path

    def _stable(rows):
        return [{k: v for k, v in row.items() if k not in volatile} for row in rows]
    assert _stable(serial_rows) == _stable(parallel_rows)
    # the registry order, each flow's own judge order: identical regardless
    # of which worker happened to finish first
    assert [(r["flow"], r["judge"]) for r in serial_rows] == \
        [(r["flow"], r["judge"]) for r in parallel_rows]


# --- the real judge's column (SP8b), proved on the fake ----------------------------------------

_TWO_FLOWS = ("post_form", "lever_single_park")


def _real(args: list, cap: str, timeout: int = 90) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    env["AUTO_APPLY_RECORD_USD_CAP"] = cap
    return subprocess.run([sys.executable, str(REPO / "scripts" / "apply_matrix.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
                          timeout=timeout, env=env)


def test_a_dry_recording_of_the_real_column_replays_with_no_miss(_browser, tmp_path):
    committed = h.REAL_CACHE.read_bytes() if h.REAL_CACHE.is_file() else None
    out_json = tmp_path / "dry.json"
    # an empty cache of its own: the committed one already answers both flows
    proc = _real(["--real", "dry", "--real-cache", str(tmp_path / "matrix_cache.json"),
                  "--flows", ",".join(_TWO_FLOWS), "--json", str(out_json)], "1.00")
    assert proc.returncode == 0, proc.stderr
    m = re.search(r"; cache (.+matrix_cache\.json)$", proc.stdout, re.M)
    assert m, proc.stdout
    cache = Path(m.group(1))
    try:
        assert cache.is_file() and cache != h.REAL_CACHE
        assert "real judge (dry): 2 of 2 flow(s)" in proc.stdout
        assert "estimated live requests" in proc.stdout
        data = json.loads(out_json.read_text(encoding="utf-8"))
        assert sorted(r["flow"] for r in data["results"]) == sorted(_TWO_FLOWS)
        assert data["unrecorded"] == [] and data["requests"] > 0
        assert all(r["judge"] == "real" and r["reads"] for r in data["results"])
        # every request the recording made replays, on another port, in a
        # worker of its own
        proc = _real(["--real", "replay", "--real-cache", str(cache), "--flows",
                      ",".join(_TWO_FLOWS), "--seeds", "0", "--jobs", "2"], "1.00")
        assert proc.returncode == 0, proc.stderr
        assert "0 miss(es) over 2 flow(s)" in proc.stdout, proc.stdout
        assert "real 100.0% over 2 flows" in proc.stdout, proc.stdout
    finally:
        shutil.rmtree(cache.parent, ignore_errors=True)
    assert (h.REAL_CACHE.read_bytes() if h.REAL_CACHE.is_file() else None) == committed


def test_the_real_column_starts_no_flow_past_the_cap(_browser, tmp_path):
    # a cap under one request's estimate: nothing is asked, and every flow
    # is listed as left unrecorded (over an empty cache: the committed one
    # would answer both flows without a request)
    proc = _real(["--real", "dry", "--real-cache", str(tmp_path / "matrix_cache.json"),
                  "--flows", ",".join(_TWO_FLOWS)], "0.000001")
    assert proc.returncode == 0, proc.stderr
    assert "AUTO_APPLY_RECORD_USD_CAP reached" in proc.stderr
    assert "left unrecorded: lever_single_park, post_form" in proc.stderr
    assert "real judge (dry): 0 of 2 flow(s)" in proc.stdout
    assert "estimated live requests 0" in proc.stdout
    m = re.search(r"; cache (.+matrix_cache\.json)$", proc.stdout, re.M)
    if m:
        shutil.rmtree(Path(m.group(1)).parent, ignore_errors=True)


# --- the timeout path, with no real worker at all --------------------------------------------

class _FakeQueue:
    def get(self, timeout):     # noqa: A002  (mirrors multiprocessing.Queue.get's own signature)
        import queue
        raise queue.Empty


class _FakeProc:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_next_message_waits_out_a_still_running_worker_to_its_deadline():
    started = time.monotonic()
    deadline = started + 0.3
    assert apply_matrix._next_message(_FakeQueue(), _FakeProc(True), deadline) is None
    assert time.monotonic() - started >= 0.3


def test_next_message_gives_up_promptly_once_a_dead_worker_has_nothing_left():
    started = time.monotonic()
    deadline = started + 5.0        # a generous deadline the dead-worker path must not use
    assert apply_matrix._next_message(_FakeQueue(), _FakeProc(False), deadline) is None
    assert time.monotonic() - started < 2.0


# --- a worker that raises ---------------------------------------------------------------------

def test_a_flow_missing_from_the_registry_is_a_failure_row_not_a_hang(tmp_path):
    # `apply_harness.flow` does `next(f for f in FLOWS if f.name == name)`: a
    # name outside the registry raises `StopIteration` before any browser or
    # server is touched, inside the worker; `_run_parallel` must turn that
    # into one row, never drop it and never hang waiting on it
    bogus = dataclasses.replace(h.flow("post_form"), name="__apply_matrix_test_boom__")
    results = apply_matrix._run_parallel((bogus,), (1,), True, 1, 10.0, tmp_path, verbose=False)
    assert len(results) == 2        # a row per judge it owed: fake and seed 1 (R2-M3)
    r = results[0]
    assert r.flow == "__apply_matrix_test_boom__"
    assert r.status == "failed"
    assert r.ok is False
    assert "worker failed" in r.reason and "StopIteration" in r.reason, r.reason


def test_a_crashed_worker_counts_every_run_it_owed_as_a_miss_and_fails_the_exit(
        tmp_path, monkeypatch, capsys):
    # SP6 review R2-M3: one row per judge, each naming the cause, and the
    # script exits nonzero even with no invariant broken
    bogus = dataclasses.replace(h.flow("post_form"), name="__apply_matrix_test_boom__")
    results = apply_matrix._run_parallel((bogus,), (1, 2), True, 1, 10.0, tmp_path,
                                         verbose=False)
    assert [r.judge for r in results] == ["fake", "noisy-1", "noisy-2"]
    assert all(r.status == "failed" and not r.ok and "StopIteration" in r.reason
               for r in results)
    assert apply_matrix._crashed(results) == 3
    monkeypatch.setattr(h, "FLOWS", h.FLOWS + (bogus,))
    code = apply_matrix.main(["--flows", bogus.name, "--seeds", "1", "--jobs", "2"])
    assert code == 1
    assert "lost to a crashed or hung worker" in capsys.readouterr().err


def test_default_flow_timeout_scales_with_the_judge_count():
    base = apply_matrix._default_flow_timeout(21)
    assert base == pytest.approx(53.0 * 3.0)
    half = apply_matrix._default_flow_timeout(3)
    assert half < base
    assert half >= apply_matrix._MIN_FLOW_TIMEOUT_S


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
