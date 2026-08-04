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
  * render_cover_letter — the bare "Defender" layout: no name banner, no
    contact line, no company line, just date / salutation / body / closing /
    name over a newtxtext (Times) preamble. compile_tex is stubbed.
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
        # Grounded relative to the stub bullets ("did a thing") so the letter
        # grounding gate (verify.letter_unseen) passes — these tests only assert
        # what rode in the prompts.
        return "I did a thing and it went well."

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


# ── template layout ───────────────────────────────────────────────────────────
def _render(monkeypatch, tmp_path, body, company="Acme", basics=None):
    monkeypatch.setattr(coverletter, "date", _FrozenDate)
    monkeypatch.setattr(coverletter.assets, "load_master", lambda: {
        "basics": basics or {"name": "Jane Doe", "email": "jane_doe@example.com",
                             "phone": "555-555-0100",
                             "linkedin": "linkedin.com/in/janedoe",
                             "github": "github.com/janedoe"},
    })
    monkeypatch.setattr(
        coverletter, "compile_tex",
        lambda tex, wd: types.SimpleNamespace(ok=True, pdf_path=None, error=""))
    _, rendered = coverletter.render_cover_letter(
        body, company, tmp_path / "cl.tex", tmp_path)
    return rendered


def test_rendered_letter_preamble_is_the_defender_one(monkeypatch, tmp_path):
    rendered = _render(monkeypatch, tmp_path, "Body.")
    for line in (r"\documentclass[11pt]{article}",
                 r"\usepackage[margin=1in]{geometry}",
                 r"\usepackage{newtxtext,newtxmath}",   # Times, as on the resume
                 r"\usepackage[english]{babel}",
                 r"\usepackage[T1]{fontenc}",
                 r"\usepackage{microtype}",
                 r"\setlength{\parindent}{0pt}",
                 r"\setlength{\parskip}{9pt}",
                 r"\pagestyle{empty}"):
        assert line in rendered
    # packages the old header stack needed and this layout does not
    for gone in (r"\usepackage{lmodern}", r"\usepackage{hyperref}",
                 r"\usepackage{parskip}", r"\usepackage[utf8]{inputenc}",
                 r"\hypersetup", r"\pagenumbering"):
        assert gone not in rendered
    # left-aligned article layout — none of the `letter`-class machinery
    for cmd in ("\\address", "\\signature", "\\opening", "\\closing",
                "\\begin{letter}"):
        assert cmd not in rendered


def test_rendered_letter_has_no_banner_contact_or_company(monkeypatch, tmp_path):
    rendered = _render(monkeypatch, tmp_path, "Body.")
    # no bold name banner: the name survives only as the signature
    assert r"\textbf{Jane Doe}" not in rendered
    assert r"\large" not in rendered
    assert rendered.count("Jane Doe") == 1
    assert rendered.index("Jane Doe") > rendered.index("Sincerely,")
    # no contact line at all (phone/email/socials), escaped or raw
    assert "555-555-0100" not in rendered
    assert "example.com" not in rendered
    assert r"\textbar" not in rendered
    assert "linkedin" not in rendered.lower()
    assert "github" not in rendered.lower()
    # the company is still an argument, but nothing renders it
    assert "Acme" not in rendered


def test_rendered_letter_order_is_date_salutation_body_closing_name(monkeypatch, tmp_path):
    rendered = _render(monkeypatch, tmp_path,
                       "First paragraph.\n\nSecond paragraph.")
    order = [rendered.index(s) for s in ("July 4, 2026", "Dear Hiring Team,",
                                         "First paragraph.", "Second paragraph.",
                                         "Sincerely,", "Jane Doe")]
    assert order == sorted(order)
    # closing is a plain blank-line break, not a forced small gap
    assert "Sincerely,\n\nJane Doe" in rendered
    assert "\\\\[6pt]" not in rendered


def test_rendered_paragraphs_are_blank_line_separated_only(monkeypatch, tmp_path):
    rendered = _render(monkeypatch, tmp_path,
                       "First paragraph.\n\nSecond paragraph.")
    # parskip 9pt supplies the gap — no \medskip between paragraphs
    assert "\\medskip" not in rendered
    assert "First paragraph.\n\nSecond paragraph." in rendered
