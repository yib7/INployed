"""The cover-letter arm of the style gate: banned AI-tell phrasing never reaches
the letter.

No real LLM ever runs: compose.call (the transport coverletter uses) is
monkeypatched. Covers the body gate (repair with the strict-improvement commit
rule, the mechanical em-dash backstop on call failure, no call for a clean
body), generate_body routing its output through the gate, the ban list riding
in the generation prompt, and the tone directives not modeling banned styling.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose, coverletter  # noqa: E402


BULLETS = {"a1": "Shipped the viewer with 178 tests",
           "a2": "Cut per-run cost by 65%"}


# ── enforce_body_style ────────────────────────────────────────────────────────
def test_enforce_body_style_repairs_offender(monkeypatch):
    body = ("I cut latency by 30%, ensuring fast responses.\n\n"
            "I would bring the same care to Acme.")
    repaired = ("I cut latency by 30% so responses stay fast.\n\n"
                "I would bring the same care to Acme.")
    monkeypatch.setattr(compose, "call", lambda *a, **k: repaired)
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", body, BULLETS)
    assert out == repaired


def test_enforce_body_style_rejects_non_improving_repair(monkeypatch):
    body = "I cut latency by 30%, ensuring fast responses."
    # The "repair" still carries a participial tail -> must be rejected.
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: "I cut latency, ensuring speed.")
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", body, BULLETS)
    assert out == body


def test_enforce_body_style_em_dash_backstop_on_call_failure(monkeypatch):
    body = "I built the sandbox — no network, no secrets."

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(compose, "call", boom)
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", body, BULLETS)
    assert "—" not in out
    assert out == "I built the sandbox, no network, no secrets."


def test_enforce_body_style_clean_body_makes_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "call", lambda *a, **k: calls.append(1) or "")
    body = "I shipped the viewer with 178 tests.\n\nI would do the same at Acme."
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", body, BULLETS)
    assert out == body
    assert calls == []  # no violations -> no LLM call at all


def test_enforce_body_style_strips_em_dash_surviving_repair(monkeypatch):
    # Repair improves (drops the tail) but still carries an em dash: commit it,
    # then the mechanical backstop strips the dash so one can never print.
    body = "I cut latency by 30%, ensuring fast responses — a big win."
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: "I cut latency by 30% — a big win.")
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", body, BULLETS)
    assert "—" not in out
    assert out == "I cut latency by 30%, a big win."


# ── generate_body wiring ──────────────────────────────────────────────────────
def _fake_master(monkeypatch):
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": {"name": "Test User", "location": "NYC"}})


def test_generate_body_gates_model_output(monkeypatch):
    _fake_master(monkeypatch)
    responses = ["I admire the team — its work is robust.",   # generation (slop)
                 "I admire the team and its published work."]  # repair (clean)
    monkeypatch.setattr(compose, "call", lambda *a, **k: responses.pop(0))
    body = coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
    assert body == "I admire the team and its published work."
    assert responses == []  # both the generation and the repair call ran


def test_generate_body_prompt_carries_ban_list(monkeypatch):
    _fake_master(monkeypatch)
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
    assert compose.BANNED_PHRASING in seen["system"]


# ── the prompts themselves must not model banned styling ─────────────────────
@pytest.mark.parametrize("tone", ["professional", "concise", "enthusiastic",
                                  "impactful", "unknown-falls-back"])
def test_tone_directives_carry_no_banned_styling(tone):
    assert compose.style_violations(coverletter.tone_directive(tone)) == []
