"""Thin synchronous LLM transport for the resume tailor (Gemini only).

Gemini is reached via Vertex AI (default) or a dedicated API key, selected by
config.gemini_auth(). One public entry-point: call(system, user, tier, **kwargs).
JSON mode returns parsed Python; text mode returns a stripped string. Retries a
few times on transient errors, with backoff for 429s.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from . import config


class LLMError(RuntimeError):
    pass


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
        raise LLMError(f"Model did not return valid JSON. Got:\n{text[:500]}")


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
            return {}
        if key and key not in dicts[0]:
            return {key: dicts}          # bare array: restore the dropped wrapper
        return dicts[0]                  # [{...}]: unwrap the object
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
    """Run one Gemini generation. `tier` resolves to a concrete model id."""
    model = config.model_for(tier)
    return _call_gemini(
        system, user, model,
        json_out=json_out, temperature=temperature,
        max_output_tokens=max_output_tokens, tools=tools,
    )


def _check_creds() -> None:
    """Fail fast (no retries) when the selected auth mode has no usable credentials."""
    if config.gemini_auth() == "api_key":
        if not os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"):
            raise LLMError("RESUME_TAILOR_GEMINI_API_KEY not set (gemini_auth=api_key).")
    elif not config.GCP_PROJECT:
        raise LLMError("Vertex auth selected but GOOGLE_CLOUD_PROJECT is not set.")


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

    client = _build_client(timeout_s)
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_out else None,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    return client.models.generate_content(model=model, contents=user, config=cfg)


def _call_gemini(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Run one Gemini generation with a per-call timeout that ESCALATES across
    attempts (config.tailor_timeout_schedule(), default 60->120->180s). A timeout
    retries on the next, longer timeout; 429/quota and other transients keep the
    prior backoff. After the schedule is exhausted, raise a clear LLMError —
    timeout-specific when the last failure was a timeout."""
    _check_creds()
    schedule = config.tailor_timeout_schedule()
    last_err: Optional[Exception] = None
    timed_out = False
    for attempt, timeout_s in enumerate(schedule):
        try:
            resp = _invoke(
                system, user, model,
                json_out=json_out, temperature=temperature,
                max_output_tokens=max_output_tokens, tools=tools, timeout_s=timeout_s,
            )
            text = resp.text or ""
            if not text.strip():
                raise LLMError("empty response")
            meta = getattr(resp, "usage_metadata", None)
            USAGE.append({
                "model": model,
                "in": getattr(meta, "prompt_token_count", 0) or 0,
                "out": getattr(meta, "candidates_token_count", 0) or 0,
            })
            return _extract_json(text) if json_out else text.strip()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if _is_timeout(exc):
                timed_out = True
                continue  # escalate to the next (longer) timeout — no sleep
            timed_out = False
            err_str = str(exc).lower()
            if "429" in err_str or "quota" in err_str:
                time.sleep(60)
            else:
                time.sleep(1.5 * (attempt + 1))
    if timed_out:
        raise LLMError(
            f"Gemini call timed out after {len(schedule)} attempts "
            f"(last timeout {schedule[-1]}s, model {model}): {last_err}"
        )
    raise LLMError(f"Gemini call failed after retries ({model}): {last_err}")


