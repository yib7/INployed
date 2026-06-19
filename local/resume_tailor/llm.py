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
    from google import genai
    from google.genai import types

    if config.gemini_auth() == "api_key":
        api_key = os.environ.get("RESUME_TAILOR_GEMINI_API_KEY")
        if not api_key:
            raise LLMError("RESUME_TAILOR_GEMINI_API_KEY not set (gemini_auth=api_key).")
        client = genai.Client(api_key=api_key)
    else:
        if not config.GCP_PROJECT:
            raise LLMError("Vertex auth selected but GOOGLE_CLOUD_PROJECT is not set.")
        client = genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_LOCATION)

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_out else None,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    
    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(model=model, contents=user, config=cfg)
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
            err_str = str(exc).lower()
            if "429" in err_str or "quota" in err_str:
                time.sleep(60)
            else:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Gemini call failed after retries ({model}): {last_err}")


