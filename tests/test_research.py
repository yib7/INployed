"""resume_tailor.research.company_blurb — the one module whose output is
unverified web-search prose that reaches a shipped PDF.

Every existing test that touches it monkeypatches `company_blurb` away, so the
function itself was never executed. These pin its three branches (the
unknown-company short-circuit, the NONE sentinel, the happy path) and the two
arguments that decide how the call is made: grounding tools, and JSON mode off
because grounding and JSON mode do not mix on Vertex.

Hermetic: `research.call` is stubbed, so no Gemini call and no billing.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config, research  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    """Record every research.call() and return a canned answer."""
    recorded: list[dict] = []
    answer = {"text": "Acme builds widgets. It shipped a new line in March."}

    def fake_call(system, user, tier, **kwargs):
        recorded.append({"system": system, "user": user, "tier": tier, **kwargs})
        return answer["text"]

    monkeypatch.setattr(research, "call", fake_call)
    return recorded, answer


# ── the short-circuit: never spend a call on a company we do not have ────────

@pytest.mark.parametrize("company", ["", "unknown company", "Unknown Company",
                                     "unknown", "UNKNOWN"])
def test_unknown_company_returns_empty_without_calling(calls, company):
    recorded, _ = calls
    assert research.company_blurb(company) == ""
    assert recorded == []


# ── the NONE sentinel: the model's "I found nothing solid" answer ────────────

@pytest.mark.parametrize("reply", ["NONE", "none", "  NONE  ",
                                   "NONE\nno reliable sources found"])
def test_none_sentinel_becomes_empty(calls, reply):
    _recorded, answer = calls
    answer["text"] = reply
    assert research.company_blurb("Acme") == ""


def test_empty_model_reply_becomes_empty(calls):
    _recorded, answer = calls
    answer["text"] = ""
    assert research.company_blurb("Acme") == ""


def test_the_sentinel_check_is_a_prefix_and_swallows_a_none_opener(calls):
    """Pinning the real behaviour: the check is `.upper().startswith("NONE")`,
    so a valid blurb opening "None of the products…" is discarded as if it
    were the sentinel. Deliberately left conservative — dropping one usable
    blurb costs a JD-only cover letter, while the other direction puts the
    literal word NONE into a shipped PDF."""
    _recorded, answer = calls
    answer["text"] = "None of the products ship outside the US. Founded 2011."
    assert research.company_blurb("Acme") == ""
    # ...while the same words later in the blurb are kept
    answer["text"] = "Acme builds widgets. None ship outside the US."
    assert research.company_blurb("Acme").startswith("Acme builds")


# ── the happy path + the call's shape ────────────────────────────────────────

def test_blurb_is_stripped_and_returned(calls):
    _recorded, answer = calls
    answer["text"] = "\n  Acme builds widgets.  \n"
    assert research.company_blurb("Acme") == "Acme builds widgets."


def test_call_requests_grounding_with_json_mode_off(calls):
    recorded, _ = calls
    research.company_blurb("Acme", "Engineer")
    assert len(recorded) == 1
    c = recorded[0]
    assert c["tier"] == config.TIER_FLASH
    # grounding tools and JSON mode do not mix on Vertex
    assert c["json_out"] is False
    assert c["tools"], "the search-grounding tool must be requested"
    # llm._call_claude reduces this to allow_websearch=bool(tools), so a
    # non-empty list is the whole contract on that lane
    assert isinstance(c["tools"], list) and len(c["tools"]) == 1


def test_company_and_role_reach_the_prompt(calls):
    recorded, _ = calls
    research.company_blurb("Globex", "Data Engineer")
    user = recorded[0]["user"]
    assert "Globex" in user
    assert "Data Engineer" in user


def test_role_line_is_omitted_when_no_job_title(calls):
    recorded, _ = calls
    research.company_blurb("Globex")
    assert "Role being applied to" not in recorded[0]["user"]


def test_system_prompt_demands_the_none_sentinel_it_parses(calls):
    """The parser above only works because the prompt names the sentinel."""
    recorded, _ = calls
    research.company_blurb("Acme")
    assert "NONE" in recorded[0]["system"]


def test_a_failed_call_propagates_for_the_caller_to_absorb(calls, monkeypatch):
    """Research failure is never fatal, but that policy lives in the cover-letter
    caller: this module does not swallow the error itself."""
    def boom(*a, **k):
        raise RuntimeError("grounding unavailable")

    monkeypatch.setattr(research, "call", boom)
    with pytest.raises(RuntimeError):
        research.company_blurb("Acme")
