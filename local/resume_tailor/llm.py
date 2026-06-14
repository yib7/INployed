"""Thin synchronous Gemini transport (Vertex AI), mirroring score_jobs.py.

One client, one `call()` helper. JSON mode returns parsed Python; text mode
returns a stripped string. Retries a few times on transient errors.
"""
from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any, Optional

from google import genai
from google.genai import types

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


@lru_cache(maxsize=1)
def client() -> genai.Client:
    return genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_LOCATION)


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
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Run one generation. Returns parsed JSON if json_out else stripped text.

    tools: optional list of types.Tool (e.g. GoogleSearch grounding for the
    company-research blurb). JSON mode and tools don't mix on Vertex — callers
    using tools should take text output.
    """
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json" if json_out else None,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = client().models.generate_content(model=model, contents=user, config=cfg)
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
        except Exception as exc:  # noqa: BLE001 - retry any transient transport/parse error
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Gemini call failed after retries ({model}): {last_err}")
