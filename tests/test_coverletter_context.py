"""SP2: cover-letter tense/date context + template critique fixes.

The user graduated May 2026; without graduation/current-date context the model
writes "I am completing my studies" in July 2026. These tests pin:

  * coverletter._education_context — pure date math over the master's
    education entries (graduated vs enrolled vs Present/missing vs
    unparseable), with `today` frozen by patching the module's `date`
    attribute (never the C type) and assets.load_master monkeypatched.
  * generate_body prompts — TODAY'S DATE + EDUCATION lines in the user
    prompt; the tense rule, boilerplate-opener ban, and no-repeated-metric
    rule in the system prompt. compose.call is stubbed: no LLM ever runs.
  * render_cover_letter — the template's \\address{} now carries the escaped
    contact block (email \\ phone \\ https LinkedIn \\ https GitHub) so the
    letter is self-contained. compile_tex is stubbed.
"""
import datetime as dt
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose, coverletter  # noqa: E402


class _FrozenDate(dt.date):
    """date subclass whose today() is pinned to 2026-07-04 (after graduation)."""

    @classmethod
    def today(cls):
        return cls(2026, 7, 4)


_EDU_GRADUATED = {"school": "College of W&M", "degree": "B.S. Computer Science",
                  "dates": "2022-08 / 2026-05"}


def _freeze(monkeypatch, education, basics=None):
    monkeypatch.setattr(coverletter, "date", _FrozenDate)
    master = {"basics": basics or {"name": "Test User", "location": "NYC"},
              "education": education}
    monkeypatch.setattr(coverletter.assets, "load_master", lambda: master)


# ── _education_context ────────────────────────────────────────────────────────
def test_education_context_graduated_past_end(monkeypatch):
    _freeze(monkeypatch, [_EDU_GRADUATED])
    out = coverletter._education_context()
    assert "B.S. Computer Science" in out and "College of W&M" in out
    assert "graduated May 2026" in out
    assert "has already graduated and is available to start immediately" in out
    assert "still enrolled" not in out


def test_education_context_end_equal_to_now_counts_as_graduated(monkeypatch):
    _freeze(monkeypatch, [{"school": "State U", "degree": "M.S. Data Science",
                           "dates": "2024-08 / 2026-07"}])
    out = coverletter._education_context()
    assert "graduated July 2026" in out
    assert "still enrolled" not in out


def test_education_context_future_end_still_enrolled(monkeypatch):
    _freeze(monkeypatch, [{"school": "State U", "degree": "B.A. Economics",
                           "dates": "2023-08 / 2027-05"}])
    out = coverletter._education_context()
    assert "expected May 2027" in out
    assert "still enrolled" in out
    assert "graduated" not in out


@pytest.mark.parametrize("dates", ["2024-08 / Present", "2024-08 /", "2024-08"])
def test_education_context_present_or_missing_end_still_enrolled(monkeypatch, dates):
    _freeze(monkeypatch, [{"school": "State U", "degree": "B.S. Physics",
                           "dates": dates}])
    out = coverletter._education_context()
    assert "B.S. Physics" in out
    assert "still enrolled" in out
    assert "graduated" not in out
    assert "expected" not in out


@pytest.mark.parametrize("dates", ["sometime / whenever", "2022-08 / 2026-13", ""])
def test_education_context_unparseable_dates_make_no_claim(monkeypatch, dates):
    if not dates:
        # A missing/blank dates field is 'no end token' -> enrolled, covered
        # above; here force a garbage END token or the empty-string entry with
        # a slash so the end exists but cannot parse.
        dates = " / "
    _freeze(monkeypatch, [{"school": "State U", "degree": "B.S. Chemistry",
                           "dates": dates}])
    out = coverletter._education_context()
    assert "B.S. Chemistry" in out and "State U" in out
    assert "graduated" not in out
    assert "expected" not in out


def test_education_context_multiple_entries_joined(monkeypatch):
    _freeze(monkeypatch, [
        _EDU_GRADUATED,
        {"school": "Grad School", "degree": "M.S. AI", "dates": "2026-08 / 2028-05"},
    ])
    out = coverletter._education_context()
    assert "; " in out
    assert "graduated May 2026" in out
    assert "expected May 2028" in out and "still enrolled" in out
    # both degree labels survive the join
    assert "B.S. Computer Science" in out and "M.S. AI" in out


# ── prompt wiring ─────────────────────────────────────────────────────────────
def _capture_prompts(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        seen.setdefault("user", user)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    return seen


def test_user_prompt_carries_today_and_education(monkeypatch):
    _freeze(monkeypatch, [_EDU_GRADUATED])
    seen = _capture_prompts(monkeypatch)
    coverletter.generate_body("jd", "Engineer", "Acme", {"a1": "did a thing"})
    assert "TODAY'S DATE: July 04, 2026." in seen["user"]
    assert "EDUCATION: " + coverletter._education_context() in seen["user"]


def test_system_prompt_carries_tense_opener_and_metric_rules(monkeypatch):
    _freeze(monkeypatch, [_EDU_GRADUATED])
    seen = _capture_prompts(monkeypatch)
    coverletter.generate_body("jd", "Engineer", "Acme", {"a1": "did a thing"})
    system = seen["system"]
    # tense rule
    assert "NEVER say" in system and "completing" in system
    assert "completed" in system
    # boilerplate opener ban + first-sentence rule
    assert "I am writing to express my interest" in system
    assert "I am writing to apply" in system
    assert "FIRST sentence" in system
    # no repeated metric
    assert "same metric or number twice" in system


# ── template contact block ────────────────────────────────────────────────────
def test_rendered_letter_address_carries_escaped_contact_block(monkeypatch, tmp_path):
    monkeypatch.setattr(coverletter.assets, "load_master", lambda: {
        "basics": {"name": "Jane Doe", "email": "jane_doe@example.com",
                   "phone": "555-555-0100",
                   "linkedin": "linkedin.com/in/janedoe",
                   "github": "github.com/janedoe"},
    })
    monkeypatch.setattr(
        coverletter, "compile_tex",
        lambda tex, wd: types.SimpleNamespace(ok=True, pdf_path=None, error=""))
    _, rendered = coverletter.render_cover_letter(
        "First paragraph.\n\nSecond paragraph.", "Acme",
        tmp_path / "cl.tex", tmp_path)
    assert "\\address{}" not in rendered
    start = rendered.index("\\address{")
    block = rendered[start:rendered.index("}", start)]
    assert "jane\\_doe@example.com" in block          # to_latex escaping applied
    assert "555-555-0100" in block
    assert "https://linkedin.com/in/janedoe" in block  # full https via assets.full_url
    assert "https://github.com/janedoe" in block
    assert block.count("\\\\") == 3                    # four lines -> three breaks
