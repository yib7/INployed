"""Thin synchronous LLM transport for the resume tailor -- two providers.

Gemini (default) is reached one of three ways, selected by config.gemini_auth():
Vertex AI, a single dedicated API key, or "pool" -- the job scorer's
keypool.KeyPool over every key in GEMINI_API_KEYS, rotated and RPD-gated per
model with Vertex as the spillover. The optional "claude" provider (config.tailor_provider())
runs the headless Claude Code CLI (`claude -p`) on the user's subscription via
the pipeline/claude_cli.py transport. One public entry-point:
call(system, user, tier, **kwargs) dispatches to whichever provider is
configured. JSON mode returns parsed Python; text mode returns a stripped
string. Retries a few times on transient errors, with backoff for 429s /
rate-limit responses.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)

# Rate limits get their own retry budget, independent of the timeout schedule:
# a parallel batch (bounded to a few concurrent jobs, each making several
# Gemini calls) EXPECTS 429s on free-tier RPM quotas, and the right response
# is to hold off and continue — not to burn the 3 timeout attempts and fail
# the job. Exponential backoff (base 30s, doubling, capped at 5 min) plus
# jitter de-synchronizes concurrent jobs; a server retryDelay hint wins when
# the error carries one.
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BASE_SLEEP = 30.0
RATE_LIMIT_MAX_SLEEP = 300.0

# In "pool" auth a 429 from a free key is not something to wait out: the pool
# holds other keys, so the pair is parked or retired and the next one is tried
# IMMEDIATELY. Rotation is naturally bounded -- each 429 costs a (key, model)
# pair, and once they are gone the pool hands back its Vertex backstop -- so
# this ceiling exists only to stop a pathological loop.
#
# It is a FLOOR, not the answer: _rotation_budget sizes the real ceiling from
# the pool, because a flat 12 is smaller than the author's 3 keys x 5 models
# and made a tailor run give up with "kept hitting quota ... through 12
# rotations" on the lease immediately before Vertex would have answered.
POOL_MAX_ROTATIONS = 12

# A 503 UNAVAILABLE ("this model is currently experiencing high demand") is
# neither a timeout nor a quota error, and used to fall through to the generic
# transient branch: three attempts, 1.5s and 3s apart, every one of them against
# the model Google had just said it was short of capacity for. In pool mode the
# real remedy is a different model, so the first overload rotates with no sleep
# at all; the backoff below only starts once rotating has already failed, which
# means either the whole chain is busy or there is nothing to rotate to (a
# single-model config, or the Vertex backstop).
OVERLOAD_MAX_RETRIES = 6
OVERLOAD_BASE_SLEEP = 2.0
OVERLOAD_MAX_SLEEP = 20.0


class LLMError(RuntimeError):
    """A tailor-transport failure, optionally classified STRUCTURALLY.

    `kind` mirrors `claude_cli.ClaudeCLIError.kind` so both provider lanes
    classify a failure by how it was produced instead of by what its message
    says. That matters because these messages embed model output (up to 500
    characters of it, in the bad-JSON case), and the retry logic below decides
    what to do by substring-matching the message it is handed. A job
    description about sales quotas would otherwise turn a deterministic parse
    failure into the 429 branch and sleep out the whole backoff budget.

    Kinds: "bad_json" and "empty" (the model answered, the answer was
    unusable), "config" (missing credentials or provider). None = unclassified.
    """

    def __init__(self, *args, kind: Optional[str] = None):
        super().__init__(*args)
        self.kind = kind


# Kinds that mean "we inspected a response that already arrived and rejected
# it". Never a transport signal, so never eligible for the timeout-escalation
# or rate-limit-backoff branches no matter what the message happens to contain.
_LOCAL_KINDS = frozenset({"bad_json", "empty", "config"})


def _local_failure(exc: BaseException) -> bool:
    """True when `exc` is our own verdict on a response, not a transport error."""
    return getattr(exc, "kind", "") in _LOCAL_KINDS


# Per-process token accounting, so a run can report tier usage for cost sanity.
USAGE: list[dict] = []


def reset_usage() -> None:
    USAGE.clear()


def usage_summary() -> str:
    if not USAGE:
        return "no LLM calls recorded"
    by_model: dict[str, list[int]] = {}
    for u in USAGE:
        agg = by_model.setdefault(u["model"], [0, 0, 0])
        agg[0] += 1
        agg[1] += u["in"]
        agg[2] += u["out"]
    return " | ".join(
        f"{m}: {c} calls, {i}+{o} tok" for m, (c, i, o) in by_model.items()
    )


def _extract_json(text: str) -> Any:
    """Parse JSON, tolerating ```json fences or surrounding prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the first balanced {...} or [...] block.
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = text.find(opener), text.rfind(closer)
            if 0 <= i < j:
                try:
                    return json.loads(text[i : j + 1])
                except json.JSONDecodeError:
                    continue
        # `from None`: the message already carries the offending payload; the
        # internal JSONDecodeError chain is noise in the user-facing dialog/CLI.
        raise LLMError(f"Model did not return valid JSON. Got:\n{text[:500]}",
                       kind="bad_json") from None


def as_dict(out: Any, key: str = "") -> dict:
    """Coerce a json_out response to the OBJECT shape its prompt demanded.

    Gemini occasionally roots the answer at an ARRAY: either the object wrapped
    in a one-element array ([{...}]) or the bare array that belonged under `key`
    (the {"key": [...]} wrapper dropped). Both recover losslessly here. Any other
    root coerces to {} so the caller degrades to its no-result path — one
    bad-shape response used to kill a whole tailor job with
    "'list' object has no attribute 'get'"."""
    if isinstance(out, dict):
        return out
    if isinstance(out, list):
        dicts = [i for i in out if isinstance(i, dict)]
        if not dicts:
            log.warning("as_dict: array response held no objects (key=%r); "
                        "degrading to empty result", key)
            return {}
        if key and key not in dicts[0]:
            return {key: dicts}          # bare array: restore the dropped wrapper
        return dicts[0]                  # [{...}]: unwrap the object
    log.warning("as_dict: unexpected root type %s (key=%r); degrading to empty "
                "result", type(out).__name__, key)
    return {}


def call(
    system: str,
    user: str,
    tier: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Run one LLM generation. `tier` resolves to a concrete model id for
    whichever provider is configured (config.tailor_provider(): 'gemini'
    default, or 'claude')."""
    if config.tailor_provider() == "claude":
        return _call_claude(
            system, user, config.claude_model_for(tier),
            json_out=json_out, temperature=temperature,
            max_output_tokens=max_output_tokens, tools=tools,
        )
    model = config.model_for(tier)
    return _call_gemini(
        system, user, model, tier=tier,
        json_out=json_out, temperature=temperature,
        max_output_tokens=max_output_tokens, tools=tools,
    )


# -- pooled auth --------------------------------------------------------------
# One KeyPool per process, built on first use. The pool owns N genai clients and
# a shared RPD state file, so rebuilding it per call would be both wasteful and
# wrong (the in-memory RPM windows would reset every time).
_POOL: Any = None
_POOL_LOCK = threading.Lock()
# The model the last _invoke on THIS thread actually ran. Thread-local because
# the tailor runs its jobs on a ThreadPoolExecutor, and in pool mode the model
# is chosen inside _invoke_pooled -- too deep to return, since _invoke's return
# value is the SDK response object and callers destructure it.
_LAST = threading.local()


def _keypool():
    """Lazy import of pipeline/keypool.py -- same sys.path hop as _claude_cli().

    Lazy so llm.py stays importable where pipeline/ is not on sys.path, and so
    the two non-pool auth modes never pay for it.
    """
    import sys

    root = str(Path(config.SCRAPE_DIR) / "pipeline")
    if root not in sys.path:
        sys.path.insert(0, root)
    import keypool
    return keypool


def _pool_limits() -> dict:
    """Free-tier rpm/rpd per model, read from the scorer's scoring_config.json.

    The tailor deliberately does NOT get its own copy of these numbers: a
    free-tier allowance belongs to the model and the Google account, not to
    whichever lane happens to be calling. Whatever the Settings tab recorded for
    the scorer's stage models governs the tailor too when it names one of them;
    a tailor model the scorer never uses falls back to keypool's own table.
    """
    return _keypool().limits_from_disk(config.SCRAPE_DIR)


def _rotation_budget(model: Any, tier: Optional[str]) -> int:
    """How many pooled retirements ONE call may legitimately need.

    Sized from the pool -- free keys x the length of this tier's ranked chain,
    times the strikes a pair may take before it is retired -- so the rotation
    ceiling can never fire while the pool still has members to hand out. The
    Vertex backstop is the last of those members, which is why an undersized
    ceiling does not merely retry less: it converts "fall back to Vertex" into
    "give up". The strike factor matters because an unproven 429 only PARKS a
    pair (keypool.QUOTA_STRIKES_BEFORE_DAILY), and a slow call lets a parked
    pair come back for its second strike before the walk is over: each pair can
    cost two rotations, exactly as KeyPool.generate sizes its own budget.
    """
    kp = _keypool()
    chain = kp.ranked_models(model, config.gemini_fallback_models(tier))
    per_pair = kp.QUOTA_STRIKES_BEFORE_DAILY
    return max(POOL_MAX_ROTATIONS, _pool().free_pair_count(chain) * per_pair + 1)


def _pool():
    """The process-wide KeyPool for gemini_auth='pool'. Built once."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            kp = _keypool()
            _POOL = kp.KeyPool.from_env(
                # The scorer's own state file, on purpose: both lanes spend ONE
                # per-key daily quota, and separate counters would each believe
                # they had the whole allowance and double-spend it into real
                # 429s. UsageState.save() folds the two processes together by
                # per-key max, which is exactly this case.
                state_path=Path(config.SCRAPE_DIR) / "score_state.json",
                limits=_pool_limits(),
                # Pool clients are built once, so they cannot escalate their
                # timeout per attempt the way _build_client does. The longest
                # slot in the schedule is the honest choice: every attempt gets
                # the most generous timeout instead of the least.
                http_timeout_s=config.tailor_timeout_schedule()[-1],
            )
        return _POOL


def reset_pool() -> None:
    """Drop the cached pool so the next call rebuilds it (tests; a key change)."""
    global _POOL
    with _POOL_LOCK:
        _POOL = None


def _check_creds() -> None:
    """Fail fast (no retries) when the selected auth mode has no usable credentials."""
    auth = config.gemini_auth()
    if auth == "api_key":
        if not os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"):
            raise LLMError("RESUME_TAILOR_GEMINI_API_KEY not set (gemini_auth=api_key).",
                           kind="config")
    elif auth == "pool":
        # Either lane on its own is a usable pool: keys with no project means no
        # Vertex spillover, a project with no keys means no free tier.
        if not (os.environ.get("GEMINI_API_KEYS", "").strip()
                or os.environ.get("GEMINI_API_KEY", "").strip()
                or config.GCP_PROJECT):
            raise LLMError(
                "gemini_auth=pool needs GEMINI_API_KEYS (the job scorer's keys) "
                "or GOOGLE_CLOUD_PROJECT, and neither is set.",
                kind="config")
    elif not config.GCP_PROJECT:
        raise LLMError("Vertex auth selected but GOOGLE_CLOUD_PROJECT is not set.",
                       kind="config")


def _is_timeout(exc: Optional[BaseException]) -> bool:
    """True if exc — or anything in its cause/context chain — is a network/HTTP
    timeout (httpx ReadTimeout/ConnectTimeout, a wrapped SDK deadline, etc.).
    Errs toward True (a stray 'timeout' in the message just means we escalate the
    timeout rather than do the short transient backoff)."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if "timeout" in type(cur).__name__.lower():
            return True
        msg = str(cur).lower()
        if "timed out" in msg or "deadline exceeded" in msg or "timeout" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_rate_limit(exc: BaseException) -> bool:
    """True for a 429 / quota-exhausted error (Gemini API or Vertex).

    Structural first, as _is_overload does: the SDK's APIError sets .code and
    .status, and a 429 whose message happens to lack every keyword still has
    to back off (or, in pool mode, rotate) rather than burn a schedule slot."""
    if getattr(exc, "code", None) == 429:
        return True
    if str(getattr(exc, "status", "")).upper() == "RESOURCE_EXHAUSTED":
        return True
    msg = str(exc).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _is_overload(exc: BaseException) -> bool:
    """True for a 503 / UNAVAILABLE: the model is busy, not out of quota.

    Mirrors keypool._is_overload_error, and is duplicated for the same reason
    _is_rate_limit duplicates keypool._is_quota_error -- llm.py must stay
    importable where pipeline/ is not on sys.path, and the non-pool auth modes
    never import keypool at all.
    """
    if getattr(exc, "code", None) == 503:
        return True
    if str(getattr(exc, "status", "")).upper() == "UNAVAILABLE":
        return True
    s = str(exc).lower()
    return "503" in s and any(
        t in s for t in ("unavailable", "overloaded", "high demand"))


def _overload_delay(retries_used: int) -> float:
    """Seconds to hold off after an overload that rotating did not fix."""
    delay = min(OVERLOAD_BASE_SLEEP * (2 ** retries_used), OVERLOAD_MAX_SLEEP)
    return delay + random.uniform(0, 0.15 * delay)


def _retry_delay_hint(message: str) -> Optional[float]:
    """The server's suggested wait, parsed out of a 429 error message.

    Gemini errors embed e.g. `'retryDelay': '22s'` (API) or `retry-delay: 90`
    (proxies). None when no hint is present."""
    m = re.search(
        r"retry[_\-]?delay['\"]?\s*[:=]\s*'?\"?(\d+(?:\.\d+)?)\s*s?",
        message, re.IGNORECASE,
    )
    return float(m.group(1)) if m else None


def _rate_limit_delay(exc: BaseException, retries_used: int) -> float:
    """Seconds to hold off before the next attempt after a 429."""
    hint = _retry_delay_hint(str(exc))
    delay = hint if hint is not None else RATE_LIMIT_BASE_SLEEP * (2 ** retries_used)
    delay = min(delay, RATE_LIMIT_MAX_SLEEP)
    return delay + random.uniform(0, 0.15 * delay)  # jitter: don't retry in lockstep


def _build_client(timeout_s: float):
    """A genai client whose HTTP requests time out after `timeout_s` seconds.
    Rebuilt per attempt so each retry can use a longer timeout (the SDK takes the
    timeout in MILLISECONDS via HttpOptions)."""
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(timeout=int(timeout_s * 1000))
    if config.gemini_auth() == "api_key":
        return genai.Client(api_key=os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"),
                            http_options=http_options)
    return genai.Client(vertexai=True, project=config.GCP_PROJECT,
                        location=config.GCP_LOCATION, http_options=http_options)


def _invoke(
    system: str,
    user: str,
    model: str,
    *,
    tier: Optional[str] = None,
    json_out: bool,
    temperature: float,
    max_output_tokens: Optional[int],
    tools: Optional[list],
    timeout_s: float,
):
    """One raw `generate_content` with a bounded timeout. Returns the SDK response
    (the caller extracts text / usage). Split out so the retry/escalation logic in
    `_call_gemini` is unit-testable without a real Gemini call."""
    from google.genai import types

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_out else None,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    if config.gemini_auth() == "pool":
        return _invoke_pooled(model, user, cfg, tier=tier)
    _LAST.model = model
    client = _build_client(timeout_s)
    return client.models.generate_content(model=model, contents=user, config=cfg)


def _invoke_pooled(model: str, user: str, cfg, *, tier: Optional[str] = None):
    """One generation against a leased (key, model) pair.

    The pool is asked for a RANKED chain -- the step's own model first, then
    config.gemini_fallback_models(tier) -- and hands back whichever pair is
    usable right now. Free-tier quota is metered per (key, model), so a fallback
    model is a separate daily allowance, and a chain multiplies the free calls
    available before anything reaches the paid Vertex backstop.

    The chain is looked up BY TIER, not by model id, because in 'tiers' mode the
    three tiers are different quality/cost classes with opposite quota profiles
    (a lite allows 500 requests/day per key, a full Flash 20). A shared chain
    would let the cheap high-volume selection pass drain the scarce allowance the
    cover letter needs. A caller with no tier gets no fallbacks in 'tiers' mode --
    see config.gemini_fallback_models.

    A 429 from a FREE key is a rotation signal, not something to wait out: the
    exhausted PAIR is retired for the day -- that key keeps whatever it still has
    on the other models -- and the failure is re-raised as kind="rotate" so
    _call_gemini retries the same schedule slot immediately against the next
    pair. A 429 from the Vertex member has nothing to rotate to, so it passes
    through unchanged and lands in the normal backoff budget.
    """
    pool = _pool()
    kp = _keypool()
    chain = kp.ranked_models(model, config.gemini_fallback_models(tier))
    member, used = pool.lease_sync(chain)
    # What USAGE and every log line should name: the model that actually ran,
    # which is not necessarily the one this step asked for.
    _LAST.model = used
    try:
        resp = member["client"].models.generate_content(
            model=used, contents=user, config=cfg)
    except Exception as exc:  # noqa: BLE001
        if member.get("kind") == "free" and _is_rate_limit(exc):
            # The 429 goes WITH the report: only its payload can say whether the
            # pair is spent for the day or merely busy this minute, and guessing
            # "day" is how a whole free tier gets written off in one run.
            what = pool.mark_exhausted(member, used, exc)
            verb = ("retired for the day" if what == "day"
                    else "parked for a cooldown")
            raise LLMError(
                f"Pooled key {verb} for {used} after a quota error; "
                f"rotating to the next pair: {exc}",
                kind="rotate",
            ) from exc
        if _is_overload(exc):
            # Not a spent allowance -- mark_exhausted here would write off a
            # whole day of this pair for a spike that clears in a minute. The
            # MODEL is parked instead (every key reaches the same busy backend,
            # so the next key would collect the identical 503), and the pool
            # skips it on the re-lease. Applies to the Vertex member too: the
            # shortage belongs to the model, not to the lane.
            pool.mark_unavailable(used)
            raise LLMError(
                f"{used} returned 503 UNAVAILABLE (overloaded); parking it and "
                f"moving to the next model in the chain: {exc}",
                kind="overload",
            ) from exc
        raise
    # An answer from the pair disproves any earlier unproven 429 against it; the
    # pool only learns that if told (its own generate() does this for itself).
    pool.mark_ok(member, used)
    return resp


def _call_gemini(
    system: str,
    user: str,
    model: str,
    *,
    tier: Optional[str] = None,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Run one Gemini generation with a per-call timeout that ESCALATES across
    attempts (config.tailor_timeout_schedule(), default 60->120->180s). A timeout
    retries on the next, longer timeout. A 429/quota error does NOT consume a
    schedule attempt: it has its own RATE_LIMIT_MAX_RETRIES budget of
    exponential-backoff waits (see the constants above) and then retries the
    SAME schedule slot — batch runs must hold off on quota pressure, not fail.
    After a budget is exhausted, raise a clear LLMError naming the cause."""
    _check_creds()
    schedule = config.tailor_timeout_schedule()
    last_err: Optional[Exception] = None
    timed_out = False
    rl_used = 0    # rate-limit waits consumed (per call, across the whole schedule)
    ol_used = 0    # 503/overload attempts consumed (per call)
    rotations = 0  # pooled pairs spent mid-call (gemini_auth='pool' only)
    max_rotations = (_rotation_budget(model, tier)
                     if config.gemini_auth() == "pool" else POOL_MAX_ROTATIONS)
    idx = 0
    while idx < len(schedule):
        timeout_s = schedule[idx]
        try:
            # Cleared before every attempt, not just written after one: the value
            # is only meaningful for the attempt that set it, and a reader that
            # trusts the last writer picks up the PREVIOUS call's model whenever
            # _invoke returns without setting it. Falls back to `model` below.
            _LAST.model = None
            resp = _invoke(
                system, user, model, tier=tier,
                json_out=json_out, temperature=temperature,
                max_output_tokens=max_output_tokens, tools=tools, timeout_s=timeout_s,
            )
            text = resp.text or ""
            if not text.strip():
                raise LLMError("empty response", kind="empty")
            meta = getattr(resp, "usage_metadata", None)
            USAGE.append({
                # Not `model`: in pool mode the chain may have spilled sideways
                # to a fallback, and attributing its tokens to the requested
                # model would misreport which allowance the run actually spent.
                "model": getattr(_LAST, "model", model) or model,
                "in": getattr(meta, "prompt_token_count", 0) or 0,
                "out": getattr(meta, "candidates_token_count", 0) or 0,
            })
            return _extract_json(text) if json_out else text.strip()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            # Rotation first, and by KIND: _invoke_pooled has already retired the
            # dead key, and its message quotes the 429 that caused it — so the
            # substring classifiers below would read it as a rate limit and sleep
            # out a 30s backoff before reaching for a key that is ready now.
            if getattr(exc, "kind", "") == "rotate":
                rotations += 1
                if rotations > max_rotations:
                    raise LLMError(
                        f"Pooled Gemini keys kept hitting quota for {model} "
                        f"through {rotations - 1} rotations, which is every "
                        f"(key, model) pair this pool has; giving up: {exc}"
                    ) from exc
                log.warning("llm: %s pooled pair spent (rotation %d/%d), "
                            "trying the next one: %s", model, rotations,
                            max_rotations, exc)
                continue  # SAME schedule slot, no sleep — the next key is ready
            # Structural classification next. `local` covers the LLMErrors this
            # very try block raises (empty body, unparseable JSON) — their
            # messages carry model output, so handing them to the substring
            # classifiers below would let a JD about sales quotas or request
            # timeouts route a deterministic failure into the 429 branch.
            local = _local_failure(exc)
            # Overload before timeout and rate limit: it is the most specific of
            # the three (the SDK hands us a numeric .code) and the only one whose
            # remedy is a different model. `local` still guards it, because a
            # bad_json message carries up to 500 characters of model output and a
            # job description quoting a 503 must not park a healthy model.
            if not local and (getattr(exc, "kind", "") == "overload"
                              or _is_overload(exc)):
                if ol_used >= OVERLOAD_MAX_RETRIES:
                    raise LLMError(
                        f"Gemini kept returning 503 UNAVAILABLE (model "
                        f"overloaded) through {ol_used} attempts across the "
                        f"{model} fallback chain; giving up: {exc}"
                    ) from exc
                # The first one is free: in pool mode the model has just been
                # parked, so re-leasing lands on the next id in the chain
                # immediately and sleeping would be pure latency.
                wait = _overload_delay(ol_used - 1) if ol_used else 0.0
                log.warning("llm: %s overloaded (503) (attempt %d/%d), "
                            "sleeping %.1fs then retrying on the next model "
                            "available: %s", model, ol_used + 1,
                            OVERLOAD_MAX_RETRIES, wait, exc)
                if wait:
                    time.sleep(wait)
                ol_used += 1
                continue  # SAME schedule slot -- this was not a timeout
            if not local and _is_timeout(exc):
                timed_out = True
                log.warning("llm: %s timed out at %ss (attempt %d/%d); escalating "
                            "timeout: %s", model, timeout_s, idx + 1,
                            len(schedule), exc)
                idx += 1
                continue  # escalate to the next (longer) timeout — no sleep
            timed_out = False
            if not local and _is_rate_limit(exc):
                if rl_used >= RATE_LIMIT_MAX_RETRIES:
                    raise LLMError(
                        f"Gemini rate limit persisted through {rl_used} waits "
                        f"({model}); giving up: {exc}"
                    ) from exc
                wait = _rate_limit_delay(exc, rl_used)
                log.warning("llm: %s rate-limited (wait %d/%d), holding off "
                            "%.0fs: %s", model, rl_used + 1,
                            RATE_LIMIT_MAX_RETRIES, wait, exc)
                time.sleep(wait)
                rl_used += 1
                continue  # retry the SAME schedule slot
            wait = 1.5 * (idx + 1)
            log.warning("llm: %s transient error (attempt %d/%d), sleeping "
                        "%.1fs: %s", model, idx + 1, len(schedule), wait, exc)
            time.sleep(wait)
            idx += 1
    if timed_out:
        raise LLMError(
            f"Gemini call timed out after {len(schedule)} attempts "
            f"(last timeout {schedule[-1]}s, model {model}): {last_err}"
        )
    raise LLMError(f"Gemini call failed after retries ({model}): {last_err}")


def _claude_cli():
    """Lazy import of the claude_cli module from pipeline/ (config.SCRAPE_DIR IS
    the repo root -- same sys.path pattern as local/manual_add.py:30-34).
    Imported lazily (not at module load) so llm.py stays importable even in
    contexts where pipeline/ isn't yet on sys.path, and so tests can stub
    this function without a real claude_cli import."""
    import sys
    from pathlib import Path

    root = str(Path(config.SCRAPE_DIR) / "pipeline")
    if root not in sys.path:
        sys.path.insert(0, root)
    import claude_cli
    return claude_cli


def _invoke_claude(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool,
    tools: Optional[list],
    timeout_s: float,
):
    """One raw `claude -p` invocation with a bounded timeout. Returns a
    claude_cli.CLIResult. Split out so the retry/escalation logic in
    `_call_claude` is unit-testable without the real CLI."""
    cc = _claude_cli()
    return cc.run_claude(
        system, user, model,
        json_mode=json_out, allow_websearch=bool(tools), timeout_s=timeout_s,
    )


def _call_claude(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Run one Claude Code CLI generation, mirroring `_call_gemini`'s retry
    envelope: a per-call timeout that ESCALATES across attempts
    (config.claude_timeout_schedule(), default 180->300s -- the CLI's cold
    start + opus latency make Gemini's 60s first slot waste attempts), and a
    429/usage-limit error that reuses the SAME schedule slot through its own
    RATE_LIMIT_MAX_RETRIES budget instead of burning a timeout attempt.

    `temperature` / `max_output_tokens` are accepted for signature parity with
    `_call_gemini` and IGNORED -- the print-mode CLI exposes neither. A
    non-empty `tools` enables the CLI's WebSearch tool (the mapping
    research.py's Gemini GoogleSearch grounding uses on the Claude lane)."""
    cc = _claude_cli()
    if cc.find_claude() is None:                       # fail fast, no retries
        raise LLMError(
            "Resume tailor provider is 'claude' but the `claude` CLI is not "
            "on PATH. Install Claude Code and run `claude` once to log in, "
            "or set the provider back to 'gemini' in Settings.",
            kind="config",
        )
    schedule = config.claude_timeout_schedule()
    last_err: Optional[Exception] = None
    timed_out = False
    rl_used = 0
    idx = 0
    while idx < len(schedule):
        timeout_s = schedule[idx]
        try:
            res = _invoke_claude(
                system, user, model,
                json_out=json_out, tools=tools, timeout_s=timeout_s,
            )
            USAGE.append({
                "model": f"claude:{model}",
                "in": res.input_tokens,
                "out": res.output_tokens,
                "cache_read": res.cache_read_tokens,
                "cache_write": res.cache_write_tokens,
            })
            return _extract_json(res.text) if json_out else res.text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            kind = getattr(exc, "kind", "")
            if kind == "timeout" or (not kind and _is_timeout(exc)):
                timed_out = True
                log.warning("llm: claude %s timed out at %ss (attempt %d/%d); "
                            "escalating timeout: %s", model, timeout_s, idx + 1,
                            len(schedule), exc)
                idx += 1
                continue  # escalate to the next (longer) timeout — no sleep
            timed_out = False
            # `not kind` mirrors the timeout line above: a kinded exception was
            # already classified at the point it was raised (claude_cli runs
            # is_rate_limit_message on the real stderr), and the unkinded case
            # is the only one whose message is worth substring-matching.
            if kind == "rate_limit" or (not kind and _is_rate_limit(exc)):
                if rl_used >= RATE_LIMIT_MAX_RETRIES:
                    raise LLMError(
                        f"Claude rate/usage limit persisted through {rl_used} "
                        f"waits ({model}); giving up (a subscription window "
                        f"limit resets on its own -- retry later): {exc}"
                    ) from exc
                wait = _rate_limit_delay(exc, rl_used)
                log.warning("llm: claude %s rate-limited (wait %d/%d), "
                            "holding off %.0fs: %s", model, rl_used + 1,
                            RATE_LIMIT_MAX_RETRIES, wait, exc)
                time.sleep(wait)
                rl_used += 1
                continue  # retry the SAME schedule slot
            wait = 1.5 * (idx + 1)
            log.warning("llm: claude %s transient error (attempt %d/%d), "
                        "sleeping %.1fs: %s", model, idx + 1, len(schedule),
                        wait, exc)
            time.sleep(wait)
            idx += 1
    if timed_out:
        raise LLMError(
            f"Claude CLI timed out after {len(schedule)} attempts "
            f"(last timeout {schedule[-1]}s, model {model}): {last_err}"
        )
    raise LLMError(f"Claude call failed after retries ({model}): {last_err}")
