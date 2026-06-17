"""Thin synchronous LLM transport for the resume tailor.

Supports three backends selected via config.backend():
  - "gemini"    — Google Gemini (API Key or Vertex AI) (default)
  - "anthropic" — Anthropic Claude API
  - "openai"    — OpenAI API

One public entry-point: call(system, user, tier, **kwargs).
JSON mode returns parsed Python; text mode returns a stripped string.
Retries a few times on transient errors, with backoff for 429s.
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
    """Run one generation against the active backend.

    `tier` is a tier token (config.TIER_FLASH etc.); the concrete model is
    resolved per backend. Returns parsed JSON if json_out else stripped text.
    """
    active = config.backend()
    model = config.model_for(tier, active)
    if active == "anthropic":
        return _call_anthropic(
            system, user, model,
            json_out=json_out, temperature=temperature, max_output_tokens=max_output_tokens,
        )
    if active == "openai":
        return _call_openai(
            system, user, model,
            json_out=json_out, temperature=temperature, max_output_tokens=max_output_tokens,
        )
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

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        client = genai.Client(api_key=api_key)
    else:
        if not config.GCP_PROJECT:
            raise LLMError("Neither GEMINI_API_KEY nor GOOGLE_CLOUD_PROJECT is set.")
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


def _call_anthropic(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
) -> Any:
    try:
        import anthropic
    except ImportError:
        raise LLMError("Anthropic backend selected but 'anthropic' package not installed.")
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    
    sys_prompt = system
    if json_out:
        sys_prompt += "\n\nRespond with ONLY valid JSON -- no prose, no markdown, no code fences."

    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            resp = client.messages.create(
                model=model,
                system=sys_prompt,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_output_tokens or 4096,
                temperature=temperature,
            )
            text = resp.content[0].text
            USAGE.append({
                "model": model,
                "in": resp.usage.input_tokens,
                "out": resp.usage.output_tokens,
            })
            return _extract_json(text) if json_out else text.strip()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if getattr(exc, "status_code", 0) == 429 or "429" in str(exc):
                time.sleep(60)
            else:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Anthropic call failed after retries ({model}): {last_err}")


def _call_openai(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
) -> Any:
    try:
        import openai
    except ImportError:
        raise LLMError("OpenAI backend selected but 'openai' package not installed.")
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY not set")
    client = openai.OpenAI(api_key=api_key)
    
    kwargs = {}
    if json_out and "o1" not in model and "o3" not in model:
        kwargs["response_format"] = {"type": "json_object"}
        system += "\n\nRespond with ONLY valid JSON."
    
    if "o1" in model or "o3" in model:
        messages = [{"role": "developer", "content": system}, {"role": "user", "content": user}]
        temperature = 1.0
    else:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if max_output_tokens:
        kwargs["max_completion_tokens"] = max_output_tokens
        
    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **kwargs
            )
            text = resp.choices[0].message.content
            USAGE.append({
                "model": model,
                "in": resp.usage.prompt_tokens,
                "out": resp.usage.completion_tokens,
            })
            return _extract_json(text) if json_out else text.strip()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if getattr(exc, "status_code", 0) == 429 or "429" in str(exc):
                time.sleep(60)
            else:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"OpenAI call failed after retries ({model}): {last_err}")
