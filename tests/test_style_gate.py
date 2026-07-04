"""The deterministic style gate: banned AI-tell phrasing never reaches the page.

No real LLM ever runs: compose.call and compose._atom_payload are monkeypatched.
Covers detection (style_violations), the batched repair (strict-improvement
commit rule, verbatim exclusion) and the mechanical em-dash backstop that runs
even when the repair call fails.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose  # noqa: E402


# ── detection ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("Built a pipeline — with four stages", ["em dash"]),
    ("Built a pipeline -- with four stages", ["em dash"]),
    ("Shipped the viewer, not a prototype", ["contrast framing"]),
    ("Delivered speed rather than size", ["contrast framing"]),
    ("Ran locally instead of in the cloud", ["contrast framing"]),
    ("Cut latency, ensuring fast responses", ["participial tail"]),
    ("Indexed articles, enabling instant search", ["participial tail"]),
    ("Utilized synthetic chats for the demo", ["buzzword verb"]),
    ("Leveraging Gemini for scoring", ["buzzword verb"]),
    ("Built a robust, seamless pipeline", ["hollow intensifier"]),
    ("Shipped a powerful new dashboard", ["hollow intensifier"]),        # the prompt-vs-gate sync gap
    ("Built a world-class ingestion service", ["hollow intensifier"]),
    ("Delivered a very fast query engine", ["hollow intensifier"]),      # bare "very" filler
    ("Successfully promoted rewards sign-ups", ["hollow intensifier"]),  # if it succeeded, say the fact
    ("Handled various edge cases in the parser", ["vague quantifier"]),
    ("Fixed numerous bugs across the stack", ["vague quantifier"]),
    ("Deployed regularly to production", ["vague quantifier"]),
])
def test_style_violations_detects(text, expected):
    assert compose.style_violations(text) == expected


def test_style_violations_spares_technical_terms():
    # These read buzzword-adjacent but are real CS/stats terms (or merely CONTAIN a
    # banned word as a substring, e.g. "very" inside "every"). The deterministic gate
    # deliberately leaves them alone; flagging one would let a repair damage a correct
    # bullet. The context-sensitive words live in the prompt bans, not the gate.
    for legit in (
        "Implemented dynamic programming for the scheduler",
        "Documented every public method signature in the SDK",
        "Reported a statistically significant lift (p < 0.05)",
        "Fit a multiple regression over 12 features",
        "Built a scalable ingestion path to 40k users",
    ):
        assert compose.style_violations(legit) == [], legit


def test_style_violations_clean_bullet():
    clean = ("Rebuilt the extraction model as LBR 2.0, raising accuracy from 80% "
             "to 95% and cutting per-run cost by 65%")
    assert compose.style_violations(clean) == []


def test_style_violations_spares_legit_words():
    # "note" contains "not"; "innovate" is not "innovative"; a hyphenated range
    # is not an em dash.
    assert compose.style_violations("Noted 1-2 week cycles; kept nothing pending") == []


# ── enforce_style ─────────────────────────────────────────────────────────────
def _sel_with(gkeys):
    return {"experience": [{"name": "X", "groups": [[gk] for gk in gkeys]}],
            "projects": [], "leadership": []}


def test_enforce_style_repairs_offender(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    sel = _sel_with(["a1", "a2"])
    bullets = {
        "a1": "Cut latency by 30%, ensuring fast responses",
        "a2": "Shipped the viewer with 178 tests",
    }
    monkeypatch.setattr(compose, "call", lambda *a, **k: {
        "bullets": [{"gkey": "a1", "text": "Cut latency by 30% so responses stay fast"}]})
    changed = compose.enforce_style("jd", "Engineer", sel, bullets)
    assert changed == 1
    assert bullets["a1"] == "Cut latency by 30% so responses stay fast"
    assert bullets["a2"] == "Shipped the viewer with 178 tests"  # clean bullet untouched


def test_enforce_style_rejects_non_improving_repair(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    sel = _sel_with(["a1"])
    original = "Cut latency by 30%, ensuring fast responses"
    bullets = {"a1": original}
    # The "repair" still carries a participial tail -> must be rejected.
    monkeypatch.setattr(compose, "call", lambda *a, **k: {
        "bullets": [{"gkey": "a1", "text": "Cut latency by 30%, ensuring speed"}]})
    changed = compose.enforce_style("jd", "Engineer", sel, bullets)
    assert changed == 0
    assert bullets["a1"] == original


def test_enforce_style_em_dash_backstop_on_call_failure(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    sel = _sel_with(["a1"])
    bullets = {"a1": "Built the sandbox — no network, no secrets"}

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(compose, "call", boom)
    changed = compose.enforce_style("jd", "Engineer", sel, bullets)
    assert changed == 1
    assert "—" not in bullets["a1"]
    assert bullets["a1"] == "Built the sandbox, no network, no secrets"


def test_enforce_style_skips_verbatim_and_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "call", lambda *a, **k: calls.append(1) or {})
    sel = _sel_with(["a1"])
    bullets = {"a1": "Shipped a clean bullet with 3 facts"}
    assert compose.enforce_style("jd", "Engineer", sel, bullets) == 0
    assert calls == []  # no offenders -> no LLM call at all
