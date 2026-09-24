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
import re
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
    assert len(results) == 1
    r = results[0]
    assert r.flow == "__apply_matrix_test_boom__"
    assert r.status == "failed"
    assert r.ok is False
    assert "worker failed" in r.reason and "StopIteration" in r.reason, r.reason


def test_default_flow_timeout_scales_with_the_judge_count():
    base = apply_matrix._default_flow_timeout(21)
    assert base == pytest.approx(53.0 * 3.0)
    half = apply_matrix._default_flow_timeout(3)
    assert half < base
    assert half >= apply_matrix._MIN_FLOW_TIMEOUT_S


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
