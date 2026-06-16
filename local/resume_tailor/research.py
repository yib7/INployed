"""Grounded "why this company" research for cover letters.

One flash call with Google Search grounding (Vertex) so the cover letter can
cite real, current facts about the company instead of generic JD-only text.
Failure is never fatal — the cover letter just falls back to JD-only mode.
"""
from __future__ import annotations

from google.genai import types

from . import config
from .llm import call


def company_blurb(company: str, job_title: str = "") -> str:
    """3-5 verifiable sentences about the company (products, recent news,
    mission, scale), or "" if grounding fails or finds nothing solid."""
    if not company or company.lower() in ("unknown company", "unknown"):
        return ""
    system = (
        "You are a job-application researcher. Using web search, return 3-5 short "
        "factual sentences about the company: what it builds/sells, one piece of "
        "recent news or a recent product launch (within ~12 months), its stated "
        "mission or values, and rough scale (size/industry position) if findable. "
        "Plain text only — no markdown, no bullet points, no URLs, no marketing "
        "fluff. Output ONLY the factual sentences themselves: no preamble or "
        "introduction (do not start with phrases like 'Here is' or 'Based on my "
        "research'), no headings, no closing remarks. Every sentence must be a "
        "verifiable fact from search results; if you cannot find solid "
        "information, reply with exactly: NONE"
    )
    user = (
        f"Company: {company}"
        + (f"\nRole being applied to: {job_title}" if job_title else "")
        + "\n\nResearch this company now."
    )
    text = call(
        system,
        user,
        config.TIER_FLASH,
        json_out=False,  # grounding tools and JSON mode don't mix on Vertex
        temperature=0.2,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    if not text or text.strip().upper().startswith("NONE"):
        return ""
    return text.strip()
