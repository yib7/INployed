"""Quota-aware Gemini key pool for the job classifier.

Wraps N free-tier API-key clients plus an optional Vertex backstop behind one
async method, generate(). Free keys are rate-limited per model (RPM via a
sliding 60s window, RPD persisted across runs); when every free key is
exhausted for the requested model the call spills to the unlimited Vertex
member. Free-tier RPD resets at midnight America/Los_Angeles.

No raw API key is ever written to disk: the state file keys usage by an 8-char
SHA-256 fingerprint. Uses the native google-genai SDK only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Free-tier limits per key, per model. TPM (250k) is never binding at ~3-5k
# tokens/call, so the pool gates on RPM + RPD only.
LIMITS: dict[str, dict[str, int]] = {
    "gemini-3.1-flash-lite": {"rpm": 15, "rpd": 500},
    "gemini-3.5-flash": {"rpm": 5, "rpd": 20},
}

_PACIFIC = ZoneInfo("America/Los_Angeles")


class PoolError(RuntimeError):
    pass


def key_fingerprint(key: str) -> str:
    """8-char SHA-256 hex of an API key -- stable, non-secret, safe to persist."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def pacific_today() -> str:
    """Current date (YYYY-MM-DD) in America/Los_Angeles, where free-tier RPD resets."""
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


class UsageState:
    """Per-(key, model) requests-per-day counter, persisted as JSON.

    On load(), a stored date that isn't today (Pacific) resets all counters.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.date = pacific_today()
        self.usage: dict[str, int] = {}

    @staticmethod
    def _k(fp: str, model: str) -> str:
        return f"{fp}:{model}"

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.date = pacific_today()
        if data.get("date") == self.date and isinstance(data.get("usage"), dict):
            self.usage = {str(k): int(v) for k, v in data["usage"].items()}
        else:
            self.usage = {}

    def get(self, fp: str, model: str) -> int:
        return int(self.usage.get(self._k(fp, model), 0))

    def incr(self, fp: str, model: str, n: int = 1) -> None:
        self.usage[self._k(fp, model)] = self.get(fp, model) + n

    def set_exhausted(self, fp: str, model: str, limit: int) -> None:
        self.usage[self._k(fp, model)] = limit

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"date": self.date, "usage": self.usage}),
                encoding="utf-8",
            )
        except OSError:
            pass
