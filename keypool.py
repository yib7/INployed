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
import tempfile
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

# Conservative gate for any model not listed above (e.g. gemini-3.1-pro-preview,
# offered in the Settings UI dropdowns but never given explicit LIMITS). Without
# this, _select's `limits is None` branch handed out free keys ungated, and a
# real 429 looped with no sleep (set_exhausted was a no-op since _select never
# checked state when limits was None).
DEFAULT_LIMITS = {"rpm": 5, "rpd": 100}

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
        self.usage = {}
        if data.get("date") == self.date and isinstance(data.get("usage"), dict):
            for k, v in data["usage"].items():
                try:
                    self.usage[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue  # corrupt entry -- drop it, don't crash the whole load

    def get(self, fp: str, model: str) -> int:
        return int(self.usage.get(self._k(fp, model), 0))

    def incr(self, fp: str, model: str, n: int = 1) -> None:
        self.usage[self._k(fp, model)] = self.get(fp, model) + n

    def set_exhausted(self, fp: str, model: str, limit: int) -> None:
        self.usage[self._k(fp, model)] = limit

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"date": self.date, "usage": self.usage}, f)
                os.replace(tmp_name, str(self.path))
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        except OSError:
            pass


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(t in s for t in ("429", "quota", "resource_exhausted", "rate limit"))


class KeyPool:
    """Async scheduler over free Gemini keys plus an optional Vertex backstop.

    members: list of {"client": <genai client>, "kind": "free"|"vertex",
                      "fp": <8-char fingerprint> | None}. Free members are gated
    by LIMITS; the Vertex member is unlimited and used only when every free key
    is RPD-exhausted for the requested model.
    """

    def __init__(self, members: list[dict], state: UsageState) -> None:
        self._members = members
        self._state = state
        self._rpm: dict[tuple[int, str], deque] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._free_calls = 0
        self._vertex_calls = 0

    def stats(self) -> dict:
        return {"free_calls": self._free_calls, "vertex_calls": self._vertex_calls}

    def _select(self, model: str, limits: Optional[dict]) -> tuple[str, int, float]:
        """Pick a member. Returns (kind, idx, wait):
        ("free", idx, 0)   reserve and call a free key;
        ("vertex", idx, 0) use the Vertex backstop;
        ("wait", -1, secs) a free key has RPD left but is RPM-throttled;
        ("none", -1, 0)    nothing usable.
        Free keys are preferred; we only wait for a throttled free key or fall to
        Vertex once no free key has RPD headroom -- preserving free quota.
        """
        now = time.monotonic()
        soonest: Optional[float] = None
        vertex_idx: Optional[int] = None
        for idx, m in enumerate(self._members):
            if m["kind"] == "vertex":
                vertex_idx = idx
                continue
            if limits is None:
                return ("free", idx, 0.0)
            if self._state.get(m["fp"], model) >= limits["rpd"]:
                continue
            dq = self._rpm[(idx, model)]
            while dq and dq[0] <= now - 60.0:
                dq.popleft()
            if len(dq) < limits["rpm"]:
                return ("free", idx, 0.0)
            w = 60.0 - (now - dq[0])
            soonest = w if soonest is None else min(soonest, w)
        if soonest is not None:
            return ("wait", -1, max(0.05, soonest))
        if vertex_idx is not None:
            return ("vertex", vertex_idx, 0.0)
        return ("none", -1, 0.0)

    def _reserve(self, idx: int, model: str) -> None:
        self._rpm[(idx, model)].append(time.monotonic())
        m = self._members[idx]
        self._state.incr(m["fp"], model)
        self._state.save()

    @classmethod
    def from_env(cls, *, state_path: Path | str | None = None) -> "KeyPool":
        from google import genai
        from google.genai import types as genai_types

        # Bounded HTTP timeout on every client: without it a hung generate_content
        # call blocks forever, and with semaphores one stuck call stalls the whole
        # stage on the unattended VM (SDK takes the timeout in milliseconds).
        timeout_ms = int(os.environ.get("SCORE_HTTP_TIMEOUT_S", "120")) * 1000
        http_options = genai_types.HttpOptions(timeout=timeout_ms)

        keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
        if not keys:
            single = os.environ.get("GEMINI_API_KEY", "").strip()
            if single:
                keys = [single]
        members: list[dict] = []
        for k in keys:
            members.append(
                {
                    "client": genai.Client(api_key=k, http_options=http_options),
                    "kind": "free",
                    "fp": key_fingerprint(k),
                }
            )
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if project:
            location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip() or "us-central1"
            members.append(
                {
                    "client": genai.Client(
                        vertexai=True, project=project, location=location, http_options=http_options
                    ),
                    "kind": "vertex",
                    "fp": None,
                }
            )
        if not members:
            raise PoolError(
                "No Gemini credentials: set GEMINI_API_KEYS (or GEMINI_API_KEY) "
                "or GOOGLE_CLOUD_PROJECT."
            )
        state = UsageState(Path(state_path) if state_path else Path("score_state.json"))
        state.load()
        return cls(members, state)

    async def generate(self, *, model: str, contents: Any, config: Any) -> Any:
        limits = LIMITS.get(model, DEFAULT_LIMITS)
        transient = 0
        while True:
            async with self._lock:
                kind, idx, wait = self._select(model, limits)
                if kind == "free":
                    self._reserve(idx, model)
            if kind == "wait":
                await asyncio.sleep(wait)
                continue
            if kind == "none":
                raise PoolError(f"No usable pool member for model {model}")
            member = self._members[idx]
            try:
                resp = await member["client"].aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001
                if _is_quota_error(exc):
                    if member["kind"] == "free":
                        async with self._lock:
                            self._state.set_exhausted(
                                member["fp"], model, limits["rpd"] if limits else 0
                            )
                            self._state.save()
                        continue
                    transient += 1
                    if transient >= 4:
                        raise PoolError(f"Vertex quota error for {model}: {exc}")
                    await asyncio.sleep(60)
                    continue
                transient += 1
                if transient >= 3:
                    raise
                await asyncio.sleep(1.5 * transient)
                continue
            if member["kind"] == "free":
                self._free_calls += 1
            else:
                self._vertex_calls += 1
            return resp
