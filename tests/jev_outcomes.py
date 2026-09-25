"""The outcomes record for the record / replay harness (`jev_harness`).

In `record` and `replay` mode every runner test that uses the `jev_judge`
fixture writes one JSON line to `outcomes.jsonl` beside the cache: the test id,
every answer the judge returned (with the fake's answer to the same question
beside it), every terminal outcome the runner reached (status, reason), the
divergence text when an assertion written against the fake failed, and the
pytest result. `scripts/jev_thresholds.py` reads the file back. In `fake` mode
nothing is written.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TestRecord:
    test: str
    mode: str
    answers: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)
    misses: list[dict] = field(default_factory=list)
    divergence: str | None = None
    result: str = ""
    usd: float = 0.0
    capped: str | None = None       # the spend cap stopped a live request in the test

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def answer_row(qid: str, answer: Any, fake: Any = None) -> dict[str, Any]:
    """One answer as a plain dict: the recorded values plus the fake's
    `choice` / `noul` / `score` for the same question, when given."""
    row = {"qid": qid, "kind": answer.kind, "noul": answer.noul, "choice": answer.choice,
           "score": answer.score, "probabilities": dict(answer.probabilities),
           "confidence": answer.confidence}
    if fake is not None:
        row["fake"] = {"noul": fake.noul, "choice": fake.choice, "score": fake.score}
    return row


class OutcomesWriter:
    """Appends one JSON line per test record; `reset()` starts an empty file so
    one run is one file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def write(self, record: TestRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def read_outcomes(path: Path) -> list[dict]:
    """Every record in the file, in order; a missing file is no records."""
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
