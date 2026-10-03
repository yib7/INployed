"""Replay the runner tests against the committed Jev cache; fail on any miss or divergence.

The default suite (and CI) runs the runner tests with the fake judge, so the real
judge's recorded answers in `tests/fixtures/jev_cache/cache.json` are exercised only
when someone asks. This script asks: it runs `RUNNER_TESTS` (`tests/jev_harness.py`)
once, serially, with `AUTO_APPLY_TEST_JEV=replay`, and exits 1 when any test misses
the cache, diverges from the fake (the harness turns that assertion into an xfail),
or fails. Run it before a release:

    python scripts/replay_check.py

It never records and never spends: the mode is forced to `replay`, the record,
capture, prune and dry-run switches and the judge's key are dropped from the child's
environment, and the cache is a temp copy (so the committed cache and its
`outcomes.jsonl` are never written). A replay with no inner judge sends nothing.
Extra arguments go to pytest (`-x`, `-v`); never `-n`, which the harness refuses.

Exit 0 clean; 1 on a miss, a divergence, a failed test or a cache the run changed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import jev_harness  # noqa: E402  (puts local/ on the path)
import jev  # noqa: E402
from jev_outcomes import read_outcomes  # noqa: E402

# what would make the child record, spend, prune or capture: never passed on
DROPPED_ENV = ("AUTO_APPLY_CAPTURE_JEV", jev_harness.PRUNE_ENV, jev_harness.DRY_ENV,
               jev_harness.CAP_ENV, jev.KEY_ENV, "PYTEST_ADDOPTS",
               "PYTEST_XDIST_WORKER")


def replay_env(base: Mapping[str, str], cache: Path) -> dict[str, str]:
    """The child's environment: `base` with the replay mode forced, the cache
    pointed at `cache`, and every record/spend switch dropped."""
    env = {k: v for k, v in base.items() if k not in DROPPED_ENV}
    env[jev_harness.MODE_ENV] = "replay"
    env[jev.CACHE_ENV] = str(cache)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def pytest_command(extra: Sequence[str] = ()) -> list[str]:
    """The serial runner-test command (no -n: replay writes one outcomes file)."""
    bad = [a for a in extra if a == "-n" or a.startswith(("-n", "--numprocesses"))]
    if bad:
        raise SystemExit(f"replay_check: {bad[0]} is refused; replay runs serially "
                         f"(see conftest_jev.pytest_configure)")
    return [sys.executable, "-m", "pytest", *jev_harness.RUNNER_TESTS.split(), "-q",
            "-p", "no:cacheprovider", "--timeout=600", *extra]


def verdict(records: Sequence[Mapping], returncode: int, *,
            cache_changed: bool = False) -> tuple[bool, list[str]]:
    """(clean, lines) from the run's outcome records and pytest's exit code."""
    missed = [r for r in records if r.get("misses")]
    diverged = [r for r in records if r.get("divergence")]
    failed = [r for r in records if r.get("result") == "failed" and not r.get("misses")]
    lines = [f"replay check: {len(records)} test(s) used the judge; "
             f"{len(missed)} missed the cache, {len(diverged)} diverged from the fake, "
             f"{len(failed)} failed otherwise; pytest exit {returncode}"]
    for r in missed:
        ids = sorted({q for m in r["misses"] for q in m.get("questions", ())})
        lines.append(f"  MISS {r['test']}: {len(r['misses'])} request(s), question ids {ids}")
    for r in diverged:
        lines.append(f"  DIVERGED {r['test']}: {r['divergence']}")
    for r in failed:
        lines.append(f"  FAILED {r['test']}")
    if cache_changed:
        lines.append("  the cache copy changed during the run: a replay must only read it")
    if not records and returncode == 0:
        lines.append("  no test used the judge: the runner tests did not reach the harness")
    clean = (returncode == 0 and not missed and not diverged and not failed
             and not cache_changed and bool(records))
    if missed:
        lines.append(f"  re-record the missed tests with {jev_harness.RECORD_COMMAND} "
                     f"(a live recording: a key and a spend cap; the user runs it)")
    lines.append("replay check: " + ("PASS" if clean else "FAIL"))
    return clean, lines


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def main(argv: Sequence[str] | None = None) -> int:
    extra = list(sys.argv[1:] if argv is None else argv)
    source = jev_harness.cache_path_from(os.environ)
    if not source.is_file():
        print(f"replay_check: no cache at {source}", file=sys.stderr)
        return 1
    source_digest = _digest(source)
    work = Path(tempfile.mkdtemp(prefix="inployed-replay-check-"))
    try:
        cache = work / "cache.json"
        shutil.copy2(source, cache)
        before = _digest(cache)
        cmd = pytest_command(extra)
        print("replay_check: " + " ".join(cmd[2:]), flush=True)
        returncode = subprocess.run(cmd, cwd=REPO, env=replay_env(os.environ, cache)).returncode
        records = read_outcomes(jev_harness.outcomes_path(cache))
        changed = _digest(cache) != before or _digest(source) != source_digest
        clean, lines = verdict(records, returncode, cache_changed=changed)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n".join(lines), flush=True)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
