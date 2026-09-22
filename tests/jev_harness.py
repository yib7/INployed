"""The judge selector for the runner tests (SP8: record and replay).

`AUTO_APPLY_TEST_JEV` picks the judge every `Runner(...)` in
`tests/test_apply_run.py` and `tests/test_apply_run_boundaries.py` gets:

- unset or `fake`: `FakeJev()`, today's behaviour, nothing written anywhere.
- `record`: `ReplayJev(TypeSafeJev(), cache)`; every request the fixtures make
  is answered by the live model once and stored in the cache. Needs
  `TYPESAFE_API_KEY` in the environment (the fixture skips with the reason when
  it is unset). Live spend is measured per test through `jev.usage()` and the
  session stops recording, skipping the remaining tests, once it passes
  `AUTO_APPLY_RECORD_USD_CAP` (default 1.00 USD).
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
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "local") not in sys.path:
    sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from jev_outcomes import OutcomesWriter, TestRecord, answer_row  # noqa: E402
from jsonutil import read_json_dict  # noqa: E402

MODE_ENV = "AUTO_APPLY_TEST_JEV"
CAP_ENV = "AUTO_APPLY_RECORD_USD_CAP"
DEFAULT_CAP_USD = 1.00
MODES = ("fake", "record", "replay")
FIXTURE = "jev_judge"
RUNNER_TESTS = "tests/test_apply_run.py tests/test_apply_run_boundaries.py"


def mode_from(env: Mapping[str, str]) -> str:
    raw = (env.get(MODE_ENV) or "fake").strip().lower()
    if raw not in MODES:
        raise ValueError(f"{MODE_ENV}={raw!r}: expected one of {', '.join(MODES)}")
    return raw


def cache_path_from(env: Mapping[str, str]) -> Path:
    raw = (env.get(jev.CACHE_ENV) or "").strip()
    return Path(raw) if raw else REPO / jev.DEFAULT_CACHE


def cap_from(env: Mapping[str, str]) -> float:
    raw = (env.get(CAP_ENV) or "").strip()
    return float(raw) if raw else DEFAULT_CAP_USD


def outcomes_path(cache_path: Path) -> Path:
    return Path(cache_path).parent / "outcomes.jsonl"


def live_judge() -> jev.Jev:
    """The live judge `record` mode records through. Tests replace this."""
    return jev.TypeSafeJev()


class Observed:
    """A judge that logs every answer (with the fake's answer to the same
    question) on the current test's record and notes a replay miss on it
    before re-raising."""

    def __init__(self, inner: jev.Jev, record: TestRecord):
        self.inner = inner
        self.record = record

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, jev.Answer]:
        try:
            answers = self.inner.judge(state, questions)
        except jev.JevUnavailable as e:
            self.record.misses.append({"key": jev.ReplayJev.key_for(state, questions),
                                       "questions": sorted(questions), "error": str(e)})
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

    def __init__(self, mode: str, cache_path: Path, cap_usd: float = DEFAULT_CAP_USD,
                 env: Mapping[str, str] | None = None):
        if mode not in MODES:
            raise ValueError(f"unknown harness mode {mode!r}")
        self.mode = mode
        self.cache_path = Path(cache_path)
        self.cap_usd = float(cap_usd)
        self.env = os.environ if env is None else env
        self.replay: jev.ReplayJev | None = None
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
        return cls(mode_from(env), cache_path_from(env), cap_from(env), env=env)

    @property
    def soft(self) -> bool:
        """Record and replay mode: outcomes are written and a failed assertion
        is a recorded divergence, since the fixtures were written against the
        fake's answers."""
        return self.mode != "fake"

    def skip_reason(self) -> str | None:
        if self.stopped:
            return self.stopped
        if self.mode == "record" and not (self.env.get(jev.KEY_ENV) or "").strip():
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
        if self.mode == "record" and self.stopped is None and self.spent_usd > self.cap_usd:
            self.stopped = (f"{CAP_ENV} reached: {self.spent_usd:.2f} USD after "
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
            inner = live_judge() if self.mode == "record" else None
            self.replay = jev.ReplayJev(inner, self.cache_path)
        return self.replay

    def cached_count(self) -> int:
        return len(read_json_dict(self.cache_path))

    def miss_text(self, record: TestRecord) -> str:
        ids = sorted({q for m in record.misses for q in m["questions"]})
        return (f"Jev replay cache miss in {record.test} (fixture `{FIXTURE}`, cache "
                f"{self.cache_path}): {len(record.misses)} request(s) with question ids "
                f"{ids}. Re-record with {MODE_ENV}=record QT_QPA_PLATFORM=offscreen "
                f"python -m pytest {RUNNER_TESTS} -q (a key and a small spend), or run "
                f"the one test with -k.")


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
