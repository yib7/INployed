"""Thin synchronous LLM transport for the resume tailor.

Supports two backends selected via config.backend():
  - "vertex"  — Google Vertex AI Gemini (default)
  - "claude"  — local `claude` CLI (headless, subscription auth)

One public entry-point: call(system, user, tier, **kwargs).
JSON mode returns parsed Python; text mode returns a stripped string.
Retries a few times on transient errors.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from typing import Any, Optional

from google import genai
from google.genai import types

from . import config

CLAUDE_TIMEOUT = 180  # seconds; web search makes some calls slow


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

    tools: optional list (e.g. Vertex GoogleSearch grounding). On the Claude
    backend a non-empty tools list enables `--allowedTools WebSearch`.
    """
    model = config.model_for(tier)
    if config.backend() == "claude":
        return _call_claude(
            system, user, model,
            json_out=json_out, tools=tools, max_output_tokens=max_output_tokens,
        )
    return _call_vertex(
        system, user, model,
        json_out=json_out, temperature=temperature,
        max_output_tokens=max_output_tokens, tools=tools,
    )


def _call_vertex(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
) -> Any:
    """Vertex Gemini transport. JSON mode and tools don't mix on Vertex —
    callers using tools should take text output."""
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


def _call_claude(
    system: str,
    user: str,
    model: str,
    *,
    json_out: bool = False,
    tools: Optional[list] = None,
    max_output_tokens: Optional[int] = None,  # accepted for signature parity; CLI has no flag
) -> Any:
    """Local `claude` CLI transport (headless, subscription auth).

    Runs in a temp cwd with a full --system-prompt override so it does not
    inherit this repo's CLAUDE.md/skills/project context -- a clean generator.
    temperature is not exposed by the print-mode CLI and is ignored.
    """
    if shutil.which("claude") is None:
        raise LLMError("Claude backend selected but `claude` CLI not found on PATH")

    sys_prompt = system
    if json_out:
        sys_prompt += "\n\nRespond with ONLY valid JSON -- no prose, no markdown, no code fences."

    argv = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", sys_prompt,
        "--exclude-dynamic-system-prompt-sections",
    ]
    if tools:
        argv += ["--allowedTools", "WebSearch"]

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                argv, input=user, capture_output=True, text=True,
                encoding="utf-8", timeout=CLAUDE_TIMEOUT, cwd=tempfile.gettempdir(),
            )
            if proc.returncode != 0:
                raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or '')[:300]}")
            envelope = json.loads(proc.stdout or "{}")
            if envelope.get("is_error"):
                raise LLMError(f"claude reported error: {str(envelope.get('result'))[:300]}")
            text = (envelope.get("result") or "").strip()
            if not text:
                raise LLMError("empty response")
            usage = envelope.get("usage") or {}
            USAGE.append({
                "model": f"claude:{model}",
                "in": usage.get("input_tokens", 0) or 0,
                "out": usage.get("output_tokens", 0) or 0,
            })
            return _extract_json(text) if json_out else text
        except Exception as exc:  # noqa: BLE001 - retry any transient transport/parse error
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Claude call failed after retries ({model}): {last_err}")
