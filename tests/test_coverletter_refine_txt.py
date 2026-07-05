"""Post-batch cover-letter tweaks:

  * refine_body — a second, flash-tier polish pass (cohesion + strict grounding
    + measured tone). Best-effort: an empty result or a raising call leaves the
    draft untouched. No real LLM runs — compose.call is stubbed.
  * cover_letter_text — the clean, copy-pasteable plain-text export (same header
    and left-aligned closing as the PDF, no LaTeX, no LinkedIn/GitHub).
  * generate_body pipeline order — generation -> refine -> deterministic gate,
    so the ban gate always has the last word.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose, coverletter  # noqa: E402


BULLETS = {"a1": "Shipped the viewer with 178 tests",
           "a2": "Cut per-run cost by 65%"}


class _FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 4)


def _master(monkeypatch, basics):
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": basics})


# ── refine_body ───────────────────────────────────────────────────────────────
def test_refine_body_returns_polished_text(monkeypatch):
    monkeypatch.setattr(compose, "call", lambda *a, **k: "  polished body  ")
    out = coverletter.refine_body("jd", "Engineer", "Acme",
                                  "rough draft", BULLETS)
    assert out == "polished body"


def test_refine_body_prompt_is_grounded_and_measured(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"] = system
        seen["user"] = user
        return "ok"

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.refine_body("jd", "Engineer", "Acme", "the draft body", BULLETS,
                            tone="concise")
    # grounding: only the resume bullets, never invent
    assert "ONLY facts" in seen["system"]
    assert "never add" in seen["system"] and "invent" in seen["system"].lower()
    # measured tone (the AI-slop over-excitement the user flagged)
    assert "MEASURED" in seen["system"]
    assert "gushing" in seen["system"] or "over-the-top" in seen["system"]
    # the ban list rides along and the tone directive is honored
    assert compose.BANNED_PHRASING in seen["system"]
    assert coverletter.tone_directive("concise") in seen["system"]
    # the draft + the bullets are handed over as the only source
    assert "the draft body" in seen["user"]
    assert "Shipped the viewer with 178 tests" in seen["user"]


def test_refine_body_empty_result_keeps_draft(monkeypatch):
    monkeypatch.setattr(compose, "call", lambda *a, **k: "   ")
    out = coverletter.refine_body("jd", "Engineer", "Acme", "keep me", BULLETS)
    assert out == "keep me"


def test_refine_body_call_failure_keeps_draft(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(compose, "call", boom)
    out = coverletter.refine_body("jd", "Engineer", "Acme", "keep me", BULLETS)
    assert out == "keep me"


def test_refine_body_blank_draft_makes_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(compose, "call", lambda *a, **k: calls.append(1) or "x")
    assert coverletter.refine_body("jd", "Engineer", "Acme", "   ", BULLETS) == ""
    assert calls == []


# ── generate_body pipeline order ──────────────────────────────────────────────
def test_generate_body_runs_generation_then_refine_then_gate(monkeypatch):
    _master(monkeypatch, {"name": "Test User", "location": "NYC"})
    order = []

    def fake_call(system, user, *a, **k):
        order.append("generate")
        return "GEN"

    def fake_refine(jd, jt, co, body, bullets, **k):
        order.append(f"refine({body})")
        return "REFINED"

    def fake_gate(jd, jt, co, body, bullets, **k):
        order.append(f"gate({body})")
        return "GATED"

    monkeypatch.setattr(compose, "call", fake_call)
    monkeypatch.setattr(coverletter, "refine_body", fake_refine)
    monkeypatch.setattr(coverletter, "enforce_body_style", fake_gate)
    out = coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
    assert out == "GATED"
    # refine sees the generation output; the gate sees the refined output (last word)
    assert order == ["generate", "refine(GEN)", "gate(REFINED)"]


# ── cover_letter_text ─────────────────────────────────────────────────────────
def test_cover_letter_text_is_clean_plaintext(monkeypatch):
    monkeypatch.setattr(coverletter, "date", _FrozenDate)
    _master(monkeypatch, {"name": "Jane Doe", "phone": "555-0100",
                          "email": "jane@example.com",
                          "linkedin": "linkedin.com/in/jane",
                          "github": "github.com/jane"})
    txt = coverletter.cover_letter_text(
        "First paragraph here.\n\nSecond paragraph here.", "Acme Corp")
    # header order + content
    lines = txt.strip().split("\n\n")
    assert lines[0] == "Jane Doe"
    assert lines[1] == "555-0100 | jane@example.com"     # phone | email, no socials
    assert lines[2] == "July 4, 2026"
    assert lines[3] == "Acme Corp"
    assert lines[4] == "Dear Hiring Team,"
    assert lines[5] == "First paragraph here."
    assert lines[6] == "Second paragraph here."
    assert lines[-2] == "Sincerely,"
    assert lines[-1] == "Jane Doe"
    # no LaTeX and no social links leaked into the plain text
    assert "\\" not in txt and "textbar" not in txt
    assert "linkedin" not in txt.lower() and "github" not in txt.lower()


def test_cover_letter_text_strips_model_added_signoff(monkeypatch):
    monkeypatch.setattr(coverletter, "date", _FrozenDate)
    _master(monkeypatch, {"name": "Jane Doe", "phone": "555", "email": "j@x.co"})
    txt = coverletter.cover_letter_text(
        "Body paragraph.\n\nSincerely,\nJane Doe", "Acme")
    # the closing appears exactly once (the template's), not doubled
    assert txt.count("Sincerely,") == 1
    assert txt.strip().endswith("Sincerely,\n\nJane Doe")


def test_cover_letter_text_omits_missing_contact(monkeypatch):
    monkeypatch.setattr(coverletter, "date", _FrozenDate)
    _master(monkeypatch, {"name": "No Contact"})  # no phone/email
    txt = coverletter.cover_letter_text("Body.", "Acme")
    blocks = txt.strip().split("\n\n")
    # name, date, company, salutation, body, Sincerely, name — no empty contact line
    assert blocks[0] == "No Contact"
    assert blocks[1] == "July 4, 2026"
    assert "" not in blocks
