"""The judge selector for the runner tests (SP8: record and replay).

`AUTO_APPLY_TEST_JEV` picks the judge every `Runner(...)` in
`tests/test_apply_run.py`, `tests/test_apply_run_boundaries.py` and the
difficulty check's `tests/test_apply_assess.py` gets, and the one the
screening set's real-judge test in `tests/test_screening.py` runs every
screening question with (the runner tests, `RUNNER_TESTS`):

- unset or `fake`: `FakeJev()`, today's behaviour, nothing written anywhere.
- `record`: `ReplayJev(SpendCap(TypeSafeJev(), cap), cache)`; every request
  the fixtures make is answered by the live model once and stored in the
  cache. Needs `TYPESAFE_API_KEY` in the environment (the fixture skips with
  the reason when it is unset). `jev.SpendCap` refuses a live request whose
  estimated cost would take the session's spend past
  `AUTO_APPLY_RECORD_USD_CAP`, which a live recording must set (the run is
  refused without it); the test it stops skips with the reason, and so does
  every test after it.
- `record` with `AUTO_APPLY_RECORD_DRY=1`: the dry run. The fake answers in
  place of the live model and each request counts at its estimated size
  (`jev.DryRun`), over a temp copy of the cache: the request count and the
  spend a recording would make, and the cap at work (`jev.DRY_RECORD_CAP_USD`
  when the variable is unset), with no key, no network and the cache left as
  it was.
- `replay`: `ReplayJev(None, cache)`; a miss raises `JevUnavailable` inside
  the runner (which parks the job as failed) and the harness turns that into a
  test failure naming the fixture, the test and the re-record command.

The cache is `AUTO_APPLY_JEV_CACHE`, else `tests/fixtures/jev_cache/cache.json`.
In `record` and `replay` mode every test writes its answers and outcomes to
`outcomes.jsonl` beside the cache (see `jev_outcomes`), and an assertion that
fails becomes an xfail carrying the divergence, so one run reports every place
the real model judges the fixtures differently from the fake.

`Session` holds the per-run state; `conftest_jev` wires it into pytest as the
`jev_judge` fixture. `judge()` is the module-level factory the test helpers
call in place of `jev.FakeJev()`.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "local") not in sys.path:
    sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from jev_outcomes import OutcomesWriter, TestRecord, answer_row  # noqa: E402
from jsonutil import read_json_dict  # noqa: E402

MODE_ENV = "AUTO_APPLY_TEST_JEV"
CAP_ENV = jev.RECORD_CAP_ENV
DRY_ENV = "AUTO_APPLY_RECORD_DRY"
DRY_CAP_USD = jev.DRY_RECORD_CAP_USD
MODES = ("fake", "record", "replay")
FIXTURE = "jev_judge"
RUNNER_TESTS = ("tests/test_apply_run.py tests/test_apply_run_boundaries.py "
                "tests/test_screening.py tests/test_apply_assess.py")
# the recording command a miss points at: the script sets the cap, exports
# the key for one run and runs the runner tests serially
RECORD_COMMAND = r".\scripts\jev_record.ps1 -Target runner -Cap <USD>"
# A test whose replay recording a later phase makes carries this mark: in
# replay mode its miss skips with UNRECORDED_REASON (conftest_jev), and every
# other mode runs it as usual. The recording phase removes the mark and adds
# the module to RUNNER_TESTS.
UNRECORDED_MARK = "jev_unrecorded"
UNRECORDED_REASON = "no replay recording yet; SP8 records it"

# The day both committed caches (`cache.json`, `matrix_cache.json`) were
# recorded on. The fact catalog lists today's date (`apply_facts.build`, the
# `today` fact) in every request that carries the facts, so a request made on
# another day has another key. A record or replay run (the runner tests here,
# the matrix's real column through `apply_harness.hermetic`) reads today as
# this day, and the caches replay on any later day. A new recording keeps this
# date, or moves it together with every cache it re-records. The fake and the
# noisy judges and production keep the real date.
RECORDED_TODAY = date(2026, 9, 25)


def pinned_build(build: Callable[..., Any], today: date = RECORDED_TODAY) -> Callable[..., Any]:
    """`build` (`apply_facts.build`) with `today` as the catalog's date when
    the caller names none."""
    def _build(folder, **kw):
        kw.setdefault("today", today)
        return build(folder, **kw)
    return _build


def mode_from(env: Mapping[str, str]) -> str:
    raw = (env.get(MODE_ENV) or "fake").strip().lower()
    if raw not in MODES:
        raise ValueError(f"{MODE_ENV}={raw!r}: expected one of {', '.join(MODES)}")
    return raw


def serial_command(mode: str) -> str:
    """The one-process record/replay command. Never add -n/--numprocesses to
    it: `record` and `replay` read-modify-write a shared repo-tree cache and
    truncate/append a shared outcomes file with no cross-process lock, so
    `conftest_jev.pytest_configure` refuses to run either mode under xdist.
    A live recording names its cap."""
    cap = f"{CAP_ENV}=<USD> " if mode == "record" else ""
    return f"{cap}{MODE_ENV}={mode} QT_QPA_PLATFORM=offscreen python -m pytest {RUNNER_TESTS} -q"


def cache_path_from(env: Mapping[str, str]) -> Path:
    raw = (env.get(jev.CACHE_ENV) or "").strip()
    return Path(raw) if raw else REPO / jev.DEFAULT_CACHE


def cap_from(env: Mapping[str, str], *, live: bool = True) -> float:
    """`AUTO_APPLY_RECORD_USD_CAP`; a live recording without it is refused
    (ValueError), anything else takes `jev.DRY_RECORD_CAP_USD`."""
    return jev.record_cap(env, live=live)


def dry_from(env: Mapping[str, str]) -> bool:
    """`AUTO_APPLY_RECORD_DRY` set to 1, true or yes."""
    return (env.get(DRY_ENV) or "").strip().lower() in ("1", "true", "yes")


def dry_copy(cache_path: Path) -> Path:
    """A temp copy of the cache (an empty one when there is none) for a dry
    run to write its fake answers into."""
    tmp = Path(tempfile.mkdtemp(prefix="jev-dry-")) / Path(cache_path).name
    if Path(cache_path).is_file():
        shutil.copyfile(cache_path, tmp)
    return tmp


def outcomes_path(cache_path: Path) -> Path:
    return Path(cache_path).parent / "outcomes.jsonl"


def live_judge() -> jev.Jev:
    """The live judge `record` mode records through. Tests replace this."""
    return jev.TypeSafeJev()


class Observed:
    """A judge that logs every answer (with the fake's answer to the same
    question) on the current test's record, notes a replay miss on it before
    re-raising, and records any other failure by its type name alone (an SDK
    or HTTP message can quote the request, the URL or the key, and the
    outcomes file is shared)."""

    def __init__(self, inner: jev.Jev, record: TestRecord):
        self.inner = inner
        self.record = record

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, jev.Answer]:
        try:
            answers = self.inner.judge(state, questions)
        except jev.SpendCapReached as e:
            self.record.capped = str(e)
            raise
        except jev.JevUnavailable as e:
            self.record.misses.append({"key": jev.ReplayJev.key_for(state, questions),
                                       "questions": sorted(questions), "error": str(e)})
            raise
        except Exception as e:
            self.record.answers.append({"error": type(e).__name__})
            raise
        try:
            fake = jev.FakeJev().judge(state, questions)
        except ValueError:
            fake = {}
        for qid, a in answers.items():
            self.record.answers.append(answer_row(qid, a, fake.get(qid)))
        return answers


class Session:
    """One pytest run's harness state: the mode, the shared replay cache, the
    per-test records, the live spend and the cap."""

    def __init__(self, mode: str, cache_path: Path, cap_usd: float | None = None,
                 env: Mapping[str, str] | None = None, *, dry: bool = False):
        if mode not in MODES:
            raise ValueError(f"unknown harness mode {mode!r}")
        self.mode = mode
        self.dry = bool(dry) and mode == "record"
        self.env = os.environ if env is None else env
        # checked before the dry copy is made: a live recording without a cap
        # is refused (ValueError) before anything is written
        self.cap_usd = (cap_from(self.env, live=self.live) if cap_usd is None
                        else float(cap_usd))
        # a dry run writes its fake answers into a temp copy, never the cache
        self.source_cache = Path(cache_path)
        self.cache_path = dry_copy(cache_path) if self.dry else Path(cache_path)
        self.replay: jev.ReplayJev | None = None
        self.cap: jev.SpendCap | None = None
        self.records: dict[str, TestRecord] = {}
        self.current: TestRecord | None = None
        self.spent_usd = 0.0
        self.requests = 0
        self.stopped: str | None = None
        self._usage_before: dict | None = None
        self.writer = OutcomesWriter(outcomes_path(self.cache_path)) if self.soft else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Session:
        env = os.environ if env is None else env
        return cls(mode_from(env), cache_path_from(env), None, env=env, dry=dry_from(env))

    @property
    def live(self) -> bool:
        """A recording through the live model (record mode, not the dry run)."""
        return self.mode == "record" and not self.dry

    @property
    def soft(self) -> bool:
        """Record and replay mode: outcomes are written and a failed assertion
        is a recorded divergence, since the fixtures were written against the
        fake's answers."""
        return self.mode != "fake"

    def skip_reason(self) -> str | None:
        if self.stopped:
            return self.stopped
        if self.mode == "record" and not self.dry and not (self.env.get(jev.KEY_ENV)
                                                           or "").strip():
            return (f"{MODE_ENV}=record needs {jev.KEY_ENV} in the environment; "
                    "scripts/jev_record.ps1 exports it from .env for one run")
        return None

    def begin(self, nodeid: str) -> TestRecord:
        rec = TestRecord(test=nodeid, mode=self.mode)
        self.records[nodeid] = rec
        self.current = rec
        self._usage_before = jev.usage()
        return rec

    def end(self, nodeid: str) -> None:
        if self.current is not None and self.current.test == nodeid:
            before, after = self._usage_before or jev.usage(), jev.usage()
            delta = max(0.0, after["usd"] - before["usd"])
            self.spent_usd += delta
            self.requests += max(0, after["requests"] - before["requests"])
            self.current.usd = delta
            self.current = None
        capped = self.cap is not None and self.cap.reached
        if self.mode == "record" and self.stopped is None and (capped
                                                               or self.spent_usd > self.cap_usd):
            self.stopped = (f"{CAP_ENV} reached: {self.spent_usd:.4f} USD after "
                            f"{self.requests} live request(s), cap {self.cap_usd:.2f} USD; "
                            "the cache keeps every answer recorded so far")

    def judge(self) -> jev.Jev:
        if self.mode == "fake":
            return jev.FakeJev()
        if self.current is None:
            raise RuntimeError(
                f"jev_harness.judge() outside a test in {MODE_ENV}={self.mode}: "
                f"request the `{FIXTURE}` fixture")
        return Observed(self._replay(), self.current)

    def _replay(self) -> jev.ReplayJev:
        if self.replay is None:
            inner = None
            if self.mode == "record":
                self.cap = jev.SpendCap(jev.DryRun() if self.dry else live_judge(),
                                        self.cap_usd)
                inner = self.cap
            self.replay = jev.ReplayJev(inner, self.cache_path)
        return self.replay

    def cached_count(self) -> int:
        return len(read_json_dict(self.cache_path))

    def miss_text(self, record: TestRecord) -> str:
        ids = sorted({q for m in record.misses for q in m["questions"]})
        return (f"Jev replay cache miss in {record.test} (fixture `{FIXTURE}`, cache "
                f"{self.cache_path}): {len(record.misses)} request(s) with question ids "
                f"{ids}. Re-record with {RECORD_COMMAND} (a live recording: a key and "
                f"a spend up to the cap you name, at most what the spend ledger has "
                f"left; the script runs the tests serially, never with "
                f"-n/--numprocesses: record and replay write a shared cache and "
                f"outcomes file with no cross-process lock, see "
                f"conftest_jev.pytest_configure).")


# --- the module-level factory the test helpers call ----------------------------------

_ACTIVE: Session | None = None


def activate(session: Session) -> None:
    global _ACTIVE
    _ACTIVE = session


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None


def judge() -> jev.Jev:
    """The judge for the running test: the active session's, else the fake in
    fake mode. Any other mode outside a `jev_judge` test is a mistake."""
    if _ACTIVE is not None:
        return _ACTIVE.judge()
    if mode_from(os.environ) == "fake":
        return jev.FakeJev()
    raise RuntimeError(f"jev_harness.judge() outside a test: request the `{FIXTURE}` fixture")
