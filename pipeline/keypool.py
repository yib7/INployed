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
import logging
import os
import random
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Free-tier limits per key, per model. TPM (250k) is never binding at ~3-5k
# tokens/call, so the pool gates on RPM + RPD only.
# The Lite models are the generous tier (15 rpm / 500 rpd); every full Flash is
# 5 rpm / 20 rpd. Listing the whole family -- not just the two stage defaults --
# matters now that a stage names SEVERAL models: a fallback model is chosen in
# Settings and never gets its own rpm/rpd boxes, so an absent row here would gate
# it at DEFAULT_LIMITS and spill the overflow onto paid Vertex. Numbers from
# aistudio.google.com/rate-limit; override per model with `model_limits`.
LIMITS: dict[str, dict[str, int]] = {
    "gemini-3.1-flash-lite": {"rpm": 15, "rpd": 500},
    "gemini-3.5-flash-lite": {"rpm": 15, "rpd": 500},
    "gemini-3.5-flash": {"rpm": 5, "rpd": 20},
    "gemini-3.6-flash": {"rpm": 5, "rpd": 20},
    "gemini-3.7-flash": {"rpm": 5, "rpd": 20},
    "gemini-3.8-flash": {"rpm": 5, "rpd": 20},
}

# Conservative gate for any model not listed above (e.g. gemini-3.1-pro-preview,
# offered in the Settings UI dropdowns but never given explicit LIMITS). generate
# passes LIMITS.get(model, DEFAULT_LIMITS), so _select always has a concrete rpm/
# rpd to gate on; without this fallback an unlisted model would go ungated and a
# real 429 would loop with no RPD accounting.
DEFAULT_LIMITS = {"rpm": 5, "rpd": 100}

# A 503/UNAVAILABLE is Google saying its serving capacity for that MODEL is
# momentarily short ("high demand"), not that the caller has spent an
# allowance. Two consequences, and both are the opposite of the quota path:
# the pair must NOT be retired (set_exhausted writes off the whole day for a
# spike that clears in a minute), and retrying it is pointless -- every key
# reaches the same overloaded backend, so the cooldown is keyed by model
# alone. _select then skips it and spills to the next model in the ranked
# chain, which is the same move it already makes for a full RPM window.
OVERLOAD_COOLDOWN_S = 60.0
# Attempts a single generate() spends on 503s before surfacing the error. High
# enough to walk a ranked chain and come back to the head once, low enough that
# a broad Google outage fails instead of looping.
OVERLOAD_MAX_RETRIES = 6

# What a 429 is allowed to cost. set_exhausted stamps the DAILY ceiling, and
# _is_quota_error cannot tell a per-minute rejection from a per-day one -- so a
# single burst used to write off a whole allowance. Observed live: three keys
# showed 500/500 spent on gemini-3.5-flash-lite (1500 calls) after a run that
# made a few dozen. Unless the payload positively identifies a per-day metric,
# the first 429 only parks the PAIR; it takes a second one to retire the day.
# A per-minute blip therefore costs 90 seconds of one pair, and a genuinely
# spent pair costs two wasted calls before it is recorded.
QUOTA_COOLDOWN_S = 90.0
QUOTA_STRIKES_BEFORE_DAILY = 2

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Where the dashboard's Settings tab writes the scorer's stage models and their
# free-tier numbers. Read here, not just in score_jobs.py, because the resume
# tailor pools the SAME keys against the SAME per-model quota and so needs the
# same table -- without importing the scorer (which pulls pandas and reloads
# .env at import scope).
SCORING_CONFIG_FILE = "scoring_config.json"

# (extra-models key, env var) per stage: the ranked fallback list that follows
# the stage's primary model. See ranked_models().
_STAGE_MODELS_KEYS = (
    ("stage1_models", "SCORE_STAGE1_MODELS"),
    ("stage2_models", "SCORE_STAGE2_MODELS"),
)

# Free-tier overrides keyed by MODEL rather than by stage, as "<model> <rpm> <rpd>"
# lines. The per-stage rpm/rpd boxes cannot describe a stage that now names
# several models -- only its primary has boxes -- and two stages naming one model
# collapse to whichever was written last. This table is the general form: one row
# per model, applied after the stage numbers so an explicit row always wins.
MODEL_LIMITS_KEY = "model_limits"
MODEL_LIMITS_ENV = "SCORE_MODEL_LIMITS"

# Separators accepted between models in an env or one-line model list. Model ids
# never contain whitespace, so treating it as a separator alongside , and ; means
# an env var can carry a list in whatever shape a shell made convenient.
_LIST_SEPARATORS = ",;"


class PoolError(RuntimeError):
    pass


def _as_int(value: Any, default: int = 0) -> int:
    """int(value), or `default` for anything that will not convert.

    Deliberately forgiving: these come from a hand-editable JSON file and from
    SCORE_* environment exports, and a typo must degrade to "not configured"
    rather than raise out of a pool build.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _split_models(value: Any) -> list[str]:
    """One value split on commas, semicolons and whitespace."""
    text = str(value)
    for sep in _LIST_SEPARATORS:
        text = text.replace(sep, " ")
    return text.split()


def as_model_list(value: Any) -> list[str]:
    """A model id list from a JSON list OR a delimited string, order preserved.

    The Settings tab stores these as a JSON list of lines; an env var (the VM's
    only channel) can only carry one string, so commas, semicolons and newlines
    all separate. Blanks and duplicates are dropped -- a duplicate would make the
    pool try one model twice in a row before moving on, which is pure latency.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            parts.extend(_split_models(item))
    else:
        parts = _split_models(value)
    out: list[str] = []
    for part in parts:
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def ranked_models(primary: Any, extra: Any = None) -> list[str]:
    """[primary] + extra, deduped, in preference order.

    Order IS the policy: the pool tries every key on the first model before it
    considers the second (see KeyPool._select), so a list is a ranked fallback
    chain, not a set. The primary leads because it is the model the stage's own
    rpm/rpd boxes describe.
    """
    # as_model_list on BOTH sides, not [primary] + ...: `primary` is itself a
    # list wherever a caller already holds a ranked chain (llm.py hands one
    # straight through), and wrapping it would stringify the list into a single
    # bogus "model id".
    return as_model_list(as_model_list(primary) + as_model_list(extra))


def parse_model_limits(value: Any) -> dict:
    """{model: {"rpm": n, "rpd": n}} from "<model> <rpm> <rpd>" rows.

    Accepts whitespace, comma or colon between the three fields, so
    "gemini-3.8-flash 5 20", "gemini-3.8-flash,5,20" and "gemini-3.8-flash:5:20"
    all parse. A row that does not yield a model plus two numbers is SKIPPED
    rather than raised on: this is a hand-editable box in the Settings tab, and
    one bad line must not take a scoring run down with it.
    """
    rows: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            rows.extend(str(item).splitlines())
    elif value is not None:
        rows.extend(str(value).splitlines())
    out: dict = {}
    for row in rows:
        for chunk in row.split(";"):
            fields = [f for f in chunk.replace(",", " ").replace(":", " ").split() if f]
            if len(fields) < 3:
                continue
            model, rpm, rpd = fields[0], _as_int(fields[1], -1), _as_int(fields[2], -1)
            if rpm < 0 or rpd < 0:
                continue
            out[model] = {"rpm": rpm, "rpd": rpd}
    return out


def limits_from_config(cfg: dict) -> dict:
    """{model_id: {"rpm": n, "rpd": n}} from a scoring-config mapping.

    Keyed by MODEL ID because that is how the pool gates, and free-tier quota is
    a property of the model, not of the stage that happens to use it. Reads one
    key, `model_limits` -- see parse_model_limits for the row format.

    This used to also fold in a per-STAGE rpm/rpd pair, which was removed: a
    stage pair could only describe that stage's PRIMARY model (never the ranked
    fallbacks), and two stages naming one model collapsed last-write-wins. That
    fired in practice -- both stages pointed at gemini-3.5-flash-lite while stage
    2's boxes said 5/20, so a model allowing 15/500 was gated at 5/20 and 96% of
    its free quota went to the paid backstop. One row per model cannot collapse.
    """
    return parse_model_limits(cfg.get(MODEL_LIMITS_KEY))


def limits_from_disk(data_root: Path | str) -> dict:
    """limits_from_config() read straight off disk, environment winning.

    Mirrors score_jobs.load_scoring_config()'s env > file precedence, so a caller
    that does not own the scoring config (the resume tailor) resolves the same
    numbers the scorer will. A missing or corrupt file yields {} -- the pool then
    falls back to its own LIMITS table, which covers the whole Flash family.
    """
    try:
        raw = json.loads(
            (Path(data_root) / SCORING_CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    cfg = dict(raw) if isinstance(raw, dict) else {}
    val = os.environ.get(MODEL_LIMITS_ENV)
    if val is not None and val.strip():
        cfg[MODEL_LIMITS_KEY] = val.strip()
    return limits_from_config(cfg)


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

    # Save every N reservations rather than every call: the write happens inside
    # the pool's async lock, so per-call file IO blocks the event loop, and the
    # RPD counter tolerates a small undercount on crash (audit P2-23). Exhaustion
    # marks and pool teardown always force a save.
    _SAVE_EVERY = 10

    def __init_debounce(self) -> None:
        if not hasattr(self, "_unsaved"):
            self._unsaved = 0

    def maybe_save(self) -> None:
        self.__init_debounce()
        self._unsaved += 1
        if self._unsaved >= self._SAVE_EVERY:
            self.save()

    def _merge_disk(self) -> None:
        """Fold the on-disk counters into memory (per-key max) before writing, so
        two concurrent scoring processes don't last-writer-wins each other's RPD
        counts into avoidable 429s (audit P2-23). Both sides only increment, so
        max is the conservative union."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("date") != self.date or not isinstance(data.get("usage"), dict):
            return
        for k, v in data["usage"].items():
            try:
                self.usage[str(k)] = max(int(v), int(self.usage.get(str(k), 0)))
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        self.__init_debounce()
        self._unsaved = 0
        try:
            self._merge_disk()
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
    """True for a 429 / RESOURCE_EXHAUSTED: a spent or momentarily full quota.

    Structural first, like _is_overload_error: google-genai's APIError carries
    .code and .status, and the prose beside them is not a contract. The
    substring arm remains for an error some other layer already stringified.
    """
    if getattr(exc, "code", None) == 429:
        return True
    if str(getattr(exc, "status", "")).upper() == "RESOURCE_EXHAUSTED":
        return True
    s = str(exc).lower()
    return any(t in s for t in ("429", "quota", "resource_exhausted", "rate limit"))


def _quota_scope(exc: Exception) -> str:
    """How long a 429 deserves to be believed: "day", "minute" or "unknown".

    Gemini's 429 payloads are not uniform. Some carry a RetryInfo
    (`'retryDelay': '22s'`), which is the server saying it expects to serve
    again shortly -- a per-MINUTE window, not a spent day. Some name the metric
    that was broken, and a "PerDay" metric is the only positive evidence of
    daily exhaustion. Many carry neither, including the one that emptied the
    author's free tier: its details held nothing but a documentation link.

    "unknown" is therefore treated as the cheap case (park the pair) rather than
    the expensive one (write off the day), and the strike counter in
    _apply_quota_error is what stops a genuinely spent pair from being retried
    all day.
    """
    flat = str(exc).lower().replace("_", "").replace("-", "").replace(" ", "")
    if "perday" in flat:
        return "day"
    if "retrydelay" in flat:
        return "minute"
    return "unknown"


def _is_overload_error(exc: Exception) -> bool:
    """True for a 503 / UNAVAILABLE -- the model is busy, not out of quota.

    Structural first: google-genai's APIError carries .code (int) and .status
    (str), so a real overload is recognised without reading prose. The substring
    arm is for an error some other layer has already stringified, and demands
    BOTH the code and a capacity word so that a 503 quoted inside unrelated text
    cannot park a healthy model.
    """
    if getattr(exc, "code", None) == 503:
        return True
    if str(getattr(exc, "status", "")).upper() == "UNAVAILABLE":
        return True
    s = str(exc).lower()
    return "503" in s and any(
        t in s for t in ("unavailable", "overloaded", "high demand"))


class KeyPool:
    """Async scheduler over free Gemini keys plus an optional Vertex backstop.

    members: list of {"client": <genai client>, "kind": "free"|"vertex",
                      "fp": <8-char fingerprint> | None}. Free members are gated
    by `limits` then LIMITS; the Vertex member is unlimited and used only when
    every free key is RPD-exhausted for the requested model.

    limits: optional {model_id: {"rpm": n, "rpd": n}} from the caller's own
    config, taking precedence over the built-in LIMITS table. See _limits_for.
    """

    def __init__(self, members: list[dict], state: UsageState,
                 limits: dict | None = None) -> None:
        self._members = members
        self._state = state
        self._limits = dict(limits or {})
        self._rpm: dict[tuple[int, str], deque] = defaultdict(deque)
        # model -> monotonic deadline; see OVERLOAD_COOLDOWN_S. Not guarded by
        # either lock: a dict get/set is atomic, both entries are advisory (a
        # lost update costs one wasted call, never a miscounted quota), and
        # taking a lock here would mean the async lane's mark had to reach for
        # the sync lane's mutex.
        self._cooling: dict[str, float] = {}
        # (fp, model) -> monotonic deadline / strike count, for 429s that have
        # not proved themselves daily. Deliberately in memory and per process:
        # a guess about this minute must never outlive the run that made it,
        # the way a set_exhausted stamp does.
        self._pair_cooling: dict[tuple[str, str], float] = {}
        self._pair_strikes: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()
        # The sync lease lane (lease_sync/mark_exhausted) guards the same
        # counters with a threading.Lock, because its caller -- the resume
        # tailor -- runs its jobs on a ThreadPoolExecutor rather than an event
        # loop. The two locks do NOT exclude each other, so one KeyPool serves
        # exactly one lane: the scorer awaits generate(), the tailor leases.
        # Mixing both on a single instance would race the RPD counters.
        self._tlock = threading.Lock()
        self._free_calls = 0
        self._vertex_calls = 0
        # Saves are debounced (P2-23), so flush the tail when the process exits —
        # an unattended VM run must still persist its final RPD counts.
        # Registered once PER STATE, not per KeyPool (audit C6-8): the dashboard
        # builds a fresh pool per scoring run over one long-lived process, and an
        # unguarded atexit.register leaked a duplicate handler each time —
        # unbounded growth, and N redundant disk writes at shutdown.
        import atexit
        if not getattr(state, "_atexit_registered", False):
            atexit.register(state.save)
            state._atexit_registered = True

    def stats(self) -> dict:
        return {"free_calls": self._free_calls, "vertex_calls": self._vertex_calls}

    def _limits_for(self, model: str) -> dict:
        """RPM/RPD for `model`: the caller's configured numbers, else LIMITS.

        LIMITS is keyed by EXACT model id, so it cannot know an id chosen in the
        dashboard after this table was written. Every such choice used to land on
        DEFAULT_LIMITS with nothing logged: a fast model throttled like a slow one,
        and -- once the phantom RPD was reached -- every further call quietly spilled
        onto the paid Vertex backstop. Configured limits travel with the model choice
        so a swap carries its own numbers.

        Merged per FIELD rather than all-or-nothing: someone who knows only their RPM
        must not have it discarded for leaving RPD blank, which would be the same
        silent-downgrade failure this exists to remove. 0 and None both read as
        "not set", so a blank or negative box falls back rather than gating at zero.
        """
        base = LIMITS.get(model, DEFAULT_LIMITS)
        ov = self._limits.get(model) or {}
        return {"rpm": ov.get("rpm") or base["rpm"],
                "rpd": ov.get("rpd") or base["rpd"]}

    def _limits_by_model(self, models: list[str]) -> dict:
        return {m: self._limits_for(m) for m in models}

    def mark_unavailable(self, model: str,
                         seconds: float | None = None) -> None:
        """Park `model` after a 503 so _select skips it for a while.

        The overload counterpart of mark_exhausted, and deliberately weaker: no
        state is written to disk and no allowance is spent, because the model is
        busy rather than used up. Called by generate() on its own behalf and by
        the resume tailor, which drives its leased request itself.
        """
        if seconds is None:
            seconds = OVERLOAD_COOLDOWN_S
        self._cooling[model] = time.monotonic() + max(0.0, seconds)

    def free_pair_count(self, models: Any) -> int:
        """(free key, model) pairs this pool could retire for `models`.

        The honest size of a walk through the free tier, and what any caller
        bounding its own retry loop should use instead of a flat constant.
        """
        free = sum(1 for m in self._members if m["kind"] == "free")
        return free * max(1, len(ranked_models(models)))

    def _apply_quota_error(self, fp: str, model: str, scope: str) -> str:
        """Record a 429 against one (key, model) pair. Returns what it did.

        "day" retires the pair via set_exhausted, exactly as before. Anything
        else parks it for QUOTA_COOLDOWN_S and counts a strike; the pair is only
        written off once the strikes reach QUOTA_STRIKES_BEFORE_DAILY, so an
        ambiguous 429 can no longer spend an allowance it never proved was gone.
        Callers hold the lane's lock.
        """
        key = (fp, model)
        if scope != "day":
            strikes = self._pair_strikes.get(key, 0) + 1
            self._pair_strikes[key] = strikes
            if strikes < QUOTA_STRIKES_BEFORE_DAILY:
                self._pair_cooling[key] = time.monotonic() + QUOTA_COOLDOWN_S
                return "cooldown"
        self._state.set_exhausted(fp, model, self._limits_for(model)["rpd"])
        self._state.save()
        self._pair_cooling.pop(key, None)
        return "day"

    def mark_ok(self, member: dict, model: str) -> None:
        """Forget the strikes against a (key, model) pair that just answered.

        A strike is a guess that a 429 was a spent allowance rather than a
        per-minute blip, and a successful call on the same pair disproves it.
        Without this the counter only ever grows: the tailor's pool lives as
        long as the dashboard process and shares its keys with the scorer in
        another process, so two ordinary RPM blips hours or days apart would
        add up to a "second strike" and write off a whole day of that pair.
        generate() calls this for itself; the sync lane's caller reports its
        own success here. Not locked: a dict pop is atomic and a lost update
        costs one wasted call, never a miscounted quota.
        """
        if member.get("kind") == "free":
            self._pair_strikes.pop((member["fp"], model), None)

    def _pair_cooling_left(self, key: tuple[str, str], now: float) -> float:
        """Seconds until a 429-parked (key, model) pair is worth trying again."""
        until = self._pair_cooling.get(key)
        if until is None:
            return 0.0
        if until <= now:
            self._pair_cooling.pop(key, None)
            return 0.0
        return until - now

    def _cooling_left(self, model: str, now: float) -> float:
        """Seconds until `model` is worth trying again; 0 when it is ready."""
        until = self._cooling.get(model)
        if until is None:
            return 0.0
        if until <= now:
            self._cooling.pop(model, None)
            return 0.0
        return until - now

    def _vertex_model(self, models: list[str]) -> str:
        """Which model the paid backstop should run.

        models[0] by default: Vertex is not metered by this pool, so there is
        nothing to gain by degrading the model. A model cooling off after a 503
        is the one exception -- the shortage belongs to the model, not to the
        lane, so paying for the same refusal helps nobody. Everything cooling
        falls back to the preferred model rather than picking arbitrarily.
        """
        now = time.monotonic()
        for m in models:
            if self._cooling_left(m, now) <= 0:
                return m
        return models[0]

    def _select(self, models: list[str], limits: dict) -> tuple[str, int, str, float]:
        """Pick a (member, model) pair. Returns (kind, idx, model, wait):
        ("free", idx, model, 0)   reserve and call a free key on `model`;
        ("vertex", idx, model, 0) use the Vertex backstop;
        ("wait", -1, "", secs)    some pair has RPD left but is RPM-throttled;
        ("none", -1, "", 0)       nothing usable.

        `models` is RANKED, and the loop nesting is the whole policy: models
        outermost, keys innermost. Every key is tried on the preferred model
        before the second model is considered, so a light run stays entirely on
        the caller's first choice -- which is what keeps a scoring threshold
        calibrated against one model when volume is low.

        The lateral spill is the other half: a model whose keys all have RPD
        headroom but are momentarily RPM-full does NOT return "wait", it falls
        through to the next model. Free-tier quota is metered per (key, model),
        so a second model is a genuinely independent allowance -- sleeping out a
        60s RPM window while an equally capable model sits idle is pure waste.
        Waiting is the last resort, taken only when no pair anywhere is usable
        right now and at least one still has daily headroom; only once nothing
        has RPD left does the paid Vertex backstop come into play.

        `limits` is {model: {"rpm", "rpd"}} covering every entry in `models`.
        """
        # Free-tier RPD resets at midnight America/Los_Angeles. A process that
        # runs across that boundary must roll the counters over to the new day
        # -- reloading any state another process already wrote for today -- so
        # keys exhausted yesterday free up and today's usage is attributed under
        # today's date. Without this, a run started near midnight keeps counting
        # against the old date, under-uses free quota, and spills to paid Vertex.
        if self._state.date != pacific_today():
            self._state.load()
            # Yesterday's 429 guesses go with yesterday's counters: a strike
            # carried over would let the first 429 of the new day retire the
            # pair outright, and a park past midnight has nothing to protect.
            self._pair_strikes.clear()
            self._pair_cooling.clear()
        now = time.monotonic()
        soonest: Optional[float] = None       # RPM windows: worth waiting for
        cooling: Optional[float] = None       # 503/429 parks: worth PAYING past
        vertex_idx: Optional[int] = next(
            (i for i, m in enumerate(self._members) if m["kind"] == "vertex"), None)
        for model in models:
            lim = limits[model]
            cool = self._cooling_left(model, now)
            for idx, m in enumerate(self._members):
                if m["kind"] == "vertex":
                    continue
                if self._state.get(m["fp"], model) >= lim["rpd"]:
                    continue
                if cool > 0:
                    # Read AFTER the RPD check so a model that is both cooling
                    # and spent contributes no wait -- there would be nothing to
                    # come back to.
                    cooling = cool if cooling is None else min(cooling, cool)
                    break
                pc = self._pair_cooling_left((m["fp"], model), now)
                if pc > 0:
                    cooling = pc if cooling is None else min(cooling, pc)
                    continue
                dq = self._rpm[(idx, model)]
                while dq and dq[0] <= now - 60.0:
                    dq.popleft()
                if len(dq) < lim["rpm"]:
                    return ("free", idx, model, 0.0)
                w = 60.0 - (now - dq[0])
                soonest = w if soonest is None else min(soonest, w)
        # An RPM window is OUR OWN counter and certain to clear, so waiting it
        # out beats paying: this ordering is unchanged.
        if soonest is not None:
            return ("wait", -1, "", max(0.05, soonest))
        # A 503 park or an unproven 429 is a GUESS about the next minute, and
        # the caller asked not to stall on guesses. Vertex therefore outranks
        # them -- but only for a pool that has a Vertex member. Without one,
        # waiting still beats failing outright.
        if vertex_idx is not None:
            return ("vertex", vertex_idx, self._vertex_model(models), 0.0)
        if cooling is not None:
            return ("wait", -1, "", max(0.05, cooling))
        return ("none", -1, "", 0.0)

    def _reserve(self, idx: int, model: str) -> None:
        self._rpm[(idx, model)].append(time.monotonic())
        m = self._members[idx]
        self._state.incr(m["fp"], model)
        self._state.maybe_save()   # debounced; atexit + exhaustion marks force it

    def lease_sync(self, model: Any, *, max_wait: float = 60.0
                   ) -> tuple[dict, str]:
        """Reserve a member for `model` and hand back (member, model_used).

        generate()'s selection step, minus the request: for a caller that
        already owns a retry envelope and wants to drive the call itself (the
        resume tailor's llm.py, whose escalating timeouts and 429 backoff would
        otherwise be duplicated here). The caller runs the request against
        member["client"] with the RETURNED model id -- which is not necessarily
        the one it asked for -- and reports a quota failure back via
        mark_exhausted().

        `model` is a single id or a ranked list; both are normalised through
        ranked_models(). Selection, including the lateral spill to the next
        model, is _select's -- see it for the policy. When every pair still has
        daily headroom but is RPM-throttled, this waits for the soonest window
        to clear, up to `max_wait` in total, after which it takes the Vertex
        backstop rather than block a tailor job indefinitely.
        """
        models = ranked_models(model)
        if not models:
            raise PoolError("lease_sync called with no model")
        limits = self._limits_by_model(models)
        deadline = time.monotonic() + max(0.0, max_wait)
        while True:
            with self._tlock:
                kind, idx, chosen, wait = self._select(models, limits)
                if kind == "free":
                    self._reserve(idx, chosen)
                    self._free_calls += 1
                    return self._members[idx], chosen
                if kind == "vertex":
                    self._vertex_calls += 1
                    return self._members[idx], chosen
                if kind == "none":
                    raise PoolError(
                        f"No usable pool member for {'/'.join(models)}")
                # kind == "wait": RPD headroom exists but the RPM window is full.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    vidx = next((i for i, m in enumerate(self._members)
                                 if m["kind"] == "vertex"), None)
                    if vidx is None:
                        raise PoolError(
                            f"Every free key for {'/'.join(models)} is "
                            f"rate-limited and no Vertex backstop is configured "
                            f"(waited {max_wait:.0f}s)")
                    self._vertex_calls += 1
                    return self._members[vidx], self._vertex_model(models)
                nap = min(wait, remaining)
            time.sleep(nap)

    def mark_exhausted(self, member: dict, model: str,
                       exc: Exception | None = None) -> str:
        """Retire a free key for the rest of the day after it returned a 429.

        The sync-lane counterpart of the quota branch in generate(): stamps the
        RPD ceiling so _select stops picking this key, and forces the save so a
        concurrent process sees it. A no-op for the Vertex member, which has no
        per-day free allowance to spend.

        `model` must be the id lease_sync RETURNED, not the one requested: the
        quota that just ran out belongs to the (key, model) pair that actually
        made the call.

        Pass `exc` -- the 429 itself -- so _quota_scope can tell a proven daily
        exhaustion from a per-minute blip. Without it the pair is retired
        outright, which is the old, expensive behaviour. Returns "day" or
        "cooldown" so the caller can say which happened.
        """
        if member.get("kind") != "free":
            return "ignored"
        scope = _quota_scope(exc) if exc is not None else "day"
        with self._tlock:
            return self._apply_quota_error(member["fp"], model, scope)

    @classmethod
    def from_env(cls, *, state_path: Path | str | None = None,
                 limits: dict | None = None,
                 http_timeout_s: float | None = None) -> "KeyPool":
        from google import genai
        from google.genai import types as genai_types

        # Bounded HTTP timeout on every client: without it a hung generate_content
        # call blocks forever, and with semaphores one stuck call stalls the whole
        # stage on the unattended VM (SDK takes the timeout in milliseconds).
        # Parsed defensively: a typo (`SCORE_HTTP_TIMEOUT_S=2m`) used to raise
        # ValueError out of from_env and kill the whole nightly scoring run with a
        # raw traceback, which is the worst place to learn about a .env typo.
        try:
            timeout_s = int(str(os.environ.get("SCORE_HTTP_TIMEOUT_S", "120")).strip())
        except (TypeError, ValueError):
            timeout_s = 120
        timeout_ms = (timeout_s if timeout_s > 0 else 120) * 1000
        # An explicit argument wins over SCORE_HTTP_TIMEOUT_S: the resume tailor
        # has its own per-call timeout schedule and its clients are built once
        # here rather than per attempt, so it passes the longest slot in that
        # schedule instead of inheriting the scorer's number.
        if http_timeout_s is not None and float(http_timeout_s) > 0:
            timeout_ms = int(float(http_timeout_s) * 1000)
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
        return cls(members, state, limits=limits)

    async def generate(self, *, model: Any, contents: Any, config: Any) -> Any:
        """One generation against the pool. `model` is a single id or a RANKED
        list of interchangeable ids -- see _select for how the list is walked."""
        models = ranked_models(model)
        if not models:
            raise PoolError("generate called with no model")
        limits = self._limits_by_model(models)
        # Separate retry budgets (audit P2-1): Vertex quota/429s get 4 long-sleep
        # attempts, generic transient errors get 3 short ones. One shared counter
        # made two 429s + one transient error give up early under mixed failures.
        quota_retries = 0
        transient_retries = 0
        overload_retries = 0
        # Bounded by the POOL, not by a constant: every free 429 either parks a
        # pair or retires it, so the walk is finite, and the ceiling only has to
        # be larger than it to never fire first.
        free_quota_errors = 0
        free_quota_budget = self.free_pair_count(models) * QUOTA_STRIKES_BEFORE_DAILY + 4
        while True:
            async with self._lock:
                kind, idx, model_used, wait = self._select(models, limits)
                if kind == "free":
                    self._reserve(idx, model_used)
            if kind == "wait":
                await asyncio.sleep(wait)
                continue
            if kind == "none":
                raise PoolError(
                    f"No usable pool member for {'/'.join(models)}")
            member = self._members[idx]
            try:
                resp = await member["client"].aio.models.generate_content(
                    model=model_used, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001
                if _is_overload_error(exc):
                    # Checked before the quota branch: an overload must never be
                    # charged as spent allowance. Park the model and re-select,
                    # which walks straight to the next id in the ranked chain --
                    # no sleep, because that one is ready now.
                    self.mark_unavailable(model_used)
                    overload_retries += 1
                    if overload_retries >= OVERLOAD_MAX_RETRIES:
                        raise
                    log.warning("keypool: %s overloaded (503) (attempt %d/%d), "
                                "parking it for %.0fs and trying the next "
                                "model: %s", model_used, overload_retries,
                                OVERLOAD_MAX_RETRIES, OVERLOAD_COOLDOWN_S, exc)
                    continue
                if _is_quota_error(exc):
                    if member["kind"] == "free":
                        # The PAIR, not the key: the same key keeps whatever
                        # daily allowance it still has on the other models in
                        # the list, which is the point of ranking several. How
                        # HARD it is recorded is _apply_quota_error's call --
                        # see _quota_scope for why an unproven 429 only parks.
                        free_quota_errors += 1
                        if free_quota_errors > free_quota_budget:
                            raise PoolError(
                                f"Free keys kept returning 429 for "
                                f"{'/'.join(models)} past every (key, model) "
                                f"pair in the pool: {exc}") from exc
                        async with self._lock:
                            self._apply_quota_error(
                                member["fp"], model_used, _quota_scope(exc))
                        continue
                    quota_retries += 1
                    if quota_retries >= 4:
                        raise PoolError(
                            f"Vertex quota error for {model_used}: {exc}") from exc
                    # Jitter so parallel workers on the VM don't wake in lockstep.
                    wait = 60 + random.uniform(0, 0.5)
                    log.warning("keypool: vertex quota/429 for %s (attempt %d/4), "
                                "sleeping %.2fs: %s", model_used, quota_retries,
                                wait, exc)
                    await asyncio.sleep(wait)
                    continue
                transient_retries += 1
                if transient_retries >= 3:
                    raise
                wait = 1.5 * transient_retries + random.uniform(0, 0.5)
                log.warning("keypool: transient error for %s (attempt %d/3), "
                            "sleeping %.2fs: %s", model_used, transient_retries,
                            wait, exc)
                await asyncio.sleep(wait)
                continue
            if member["kind"] == "free":
                self._free_calls += 1
                self.mark_ok(member, model_used)
            else:
                self._vertex_calls += 1
            return resp
