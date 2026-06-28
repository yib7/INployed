"""Tests for the résumé artifact toggles + cover-letter tone knob (SP3 T3.4).

Covers:
  * coverletter.tone_directive — pure tone→instruction mapping with a
    professional fallback for unknown/empty input, and that generate_body
    injects the directive into the prompt without spending LLM credits.
  * tailor() artifact gating — ats_report / cover_letter / prep_sheet toggles,
    run fully offline by monkeypatching every LLM/compile/IO touchpoint, so the
    defaults reproduce today's behaviour (cover off, ATS on, prep on-demand).
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import coverletter  # noqa: E402
from resume_tailor import run as run_mod  # noqa: E402


# --- tone_directive ----------------------------------------------------------

def test_tone_directive_maps_each_known_tone_to_distinct_nonempty_string():
    tones = ["professional", "concise", "enthusiastic", "impactful"]
    out = {t: coverletter.tone_directive(t) for t in tones}
    for t, s in out.items():
        assert isinstance(s, str) and s.strip(), f"{t} produced empty directive"
    # all four directives are distinct
    assert len(set(out.values())) == len(tones)


def test_tone_directive_falls_back_to_professional_for_unknown_or_empty():
    pro = coverletter.tone_directive("professional")
    assert coverletter.tone_directive("snarky") == pro
    assert coverletter.tone_directive("") == pro
    assert coverletter.tone_directive(None) == pro  # type: ignore[arg-type]


def test_tone_directive_case_insensitive():
    assert coverletter.tone_directive("CONCISE") == coverletter.tone_directive("concise")


def test_generate_body_injects_tone_directive_into_prompt(monkeypatch):
    captured = {}

    def fake_call(system, user, tier, *, json_out, temperature):
        captured["system"] = system
        captured["user"] = user
        return "BODY"

    monkeypatch.setattr(coverletter.compose, "call", fake_call)
    # avoid loading the real master_experience.yaml for the display name/location
    monkeypatch.setattr(coverletter, "_display_name", lambda: "Test Name")
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": {"location": "City, ST"}})

    body = coverletter.generate_body(
        "a" * 60, "Engineer", "BigCo", {"g1": "did a thing"}, tone="concise"
    )
    assert body == "BODY"
    directive = coverletter.tone_directive("concise")
    assert directive in (captured["system"] + captured["user"])


# --- tailor() artifact gating (fully offline) --------------------------------

@pytest.fixture()
def offline_tailor(monkeypatch, tmp_path):
    """Stub every LLM/compile/IO touchpoint so tailor() runs without spending
    credits or invoking LaTeX. Returns a record dict of which artifacts fired."""
    rec = {"ats": 0, "cover": 0, "prep": 0, "apply": 0}

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    sel = {"experience": [{"name": "BigCo", "groups": [["a"]]}],
           "projects": [], "leadership": []}
    bullets = {"a": "did a thing with measurable impact and clear results"}

    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: True)
    monkeypatch.setattr(run_mod.compose, "select", lambda *a, **k: sel)
    monkeypatch.setattr(run_mod.compose, "inject_verbatim", lambda *a, **k: {})
    monkeypatch.setattr(run_mod.compose, "block_briefs", lambda *a, **k: {})
    monkeypatch.setattr(run_mod, "_resolve_bullets", lambda *a, **k: dict(bullets))
    monkeypatch.setattr(run_mod, "_trim_to_caps", lambda *a, **k: None)
    monkeypatch.setattr(run_mod.compose, "compress_skills", lambda *a, **k: ["Python"])
    monkeypatch.setattr(run_mod.output, "resolve_dir", lambda *a, **k: out_dir)
    monkeypatch.setattr(run_mod.output, "resume_filename", lambda: "resume.pdf")
    monkeypatch.setattr(run_mod.output, "cover_filename", lambda: "cover.pdf")

    # llm usage helpers are no-ops here
    monkeypatch.setattr(run_mod.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(run_mod.llm, "usage_summary", lambda: "0 tokens")

    # enforce_one_page returns a fake ok result + a real on-disk pdf to copy
    pdf = tmp_path / "compiled.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = types.SimpleNamespace(ok=True, pdf_path=pdf, error="", log_tail="")

    def fake_enforce(sel_, bullets_, skills_, tex_path, tmp_, jd, on_status=None):
        Path(tex_path).write_text("\\resumeItem{did a thing}", encoding="utf-8")
        return result, dict(bullets_), "\\documentclass{article}"

    monkeypatch.setattr(run_mod, "enforce_one_page", fake_enforce)

    def fake_ats(jd, pdf_path, out):
        rec["ats"] += 1
        return 0.5

    monkeypatch.setattr(run_mod.ats, "write_report", fake_ats)

    def fake_cover_body(*a, **k):
        rec["cover"] += 1
        return "cover body"

    cl_result = types.SimpleNamespace(ok=True, pdf_path=pdf, error="")
    monkeypatch.setattr(run_mod.coverletter, "generate_body", fake_cover_body)
    monkeypatch.setattr(run_mod.coverletter, "render_cover_letter",
                        lambda *a, **k: (cl_result, ""))
    monkeypatch.setattr(run_mod.research, "company_blurb", lambda *a, **k: "")

    def fake_apply(*a, **k):
        rec["apply"] += 1

    monkeypatch.setattr(run_mod.apply_data, "write", fake_apply)

    # prep sheet lives in run.generate_prep_sheet (imported lazily) — patch the
    # source module so the import inside tailor() picks up the stub.
    from resume_tailor import prep as prep_mod

    def fake_prep(job, out=None):
        rec["prep"] += 1
        return out_dir / "interview_prep.md"

    monkeypatch.setattr(prep_mod, "generate_prep_sheet", fake_prep)

    return rec


_JOB = {"company_name": "BigCo", "job_title": "Engineer",
        "job_description": "x" * 200, "url": "http://x"}


def test_defaults_reproduce_today_behaviour(offline_tailor):
    run_mod.tailor(_JOB)
    assert offline_tailor["ats"] == 1     # ATS on by default
    assert offline_tailor["cover"] == 0   # cover letter off by default
    assert offline_tailor["prep"] == 0    # prep on-demand by default
    assert offline_tailor["apply"] == 1   # apply_data always written


def test_ats_report_false_skips_write_report(offline_tailor):
    run_mod.tailor(_JOB, ats_report=False)
    assert offline_tailor["ats"] == 0


def test_cover_letter_true_generates_cover(offline_tailor):
    run_mod.tailor(_JOB, cover_letter=True)
    assert offline_tailor["cover"] == 1


def test_prep_sheet_true_generates_prep(offline_tailor):
    run_mod.tailor(_JOB, prep_sheet=True)
    assert offline_tailor["prep"] == 1


def test_tone_threads_into_cover_letter(offline_tailor, monkeypatch):
    seen = {}

    def capture_body(jd, job_title, company, bullets, research="", tone="professional"):
        seen["tone"] = tone
        return "body"

    monkeypatch.setattr(run_mod.coverletter, "generate_body", capture_body)
    run_mod.tailor(_JOB, cover_letter=True, tone="impactful")
    assert seen["tone"] == "impactful"
