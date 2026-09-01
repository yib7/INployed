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
from resume_tailor import prep as prep_mod  # noqa: E402
from resume_tailor import run as run_mod  # noqa: E402
from resume_tailor.compile import CompileResult  # noqa: E402


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
        return "Body"          # sentence case: see P2-4 in verify.py

    monkeypatch.setattr(coverletter.compose, "call", fake_call)
    # avoid loading the real master_experience.yaml for the display name/location
    monkeypatch.setattr(coverletter, "_display_name", lambda: "Test Name")
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": {"location": "City, ST"}})

    body = coverletter.generate_body(
        "a" * 60, "Engineer", "BigCo", {"g1": "did a thing"}, tone="concise"
    )
    assert body == "Body"
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

    def fake_render_cover(body, company, tex_path, work_dir):
        # the real render writes the .tex before compiling; tailor ships that file
        Path(tex_path).write_text("\\documentclass[11pt]{article}", encoding="utf-8")
        return cl_result, ""

    monkeypatch.setattr(run_mod.coverletter, "render_cover_letter", fake_render_cover)
    monkeypatch.setattr(run_mod.coverletter, "cover_letter_text",
                        lambda body, company: f"TXT:{body}:{company}")
    monkeypatch.setattr(run_mod.output, "cover_tex_filename", lambda: "cover.tex")
    monkeypatch.setattr(run_mod.research, "company_blurb", lambda *a, **k: "")

    def fake_apply(*a, **k):
        rec["apply"] += 1
        rec["cover_body"] = k.get("cover_body")

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


def test_tailor_dedupes_leading_verbs_with_verbatim_reserved(offline_tailor, monkeypatch):
    """run.tailor runs the no-reuse guarantee on the tailored bullets, seeding the reserved
    set from the verbatim blocks' openers (which it must not modify). Real dedupe, no network
    (reverb stubbed empty -> the deterministic in-category backstop fires)."""
    colliding = {"a1": "Built one.", "b1": "Built two."}
    monkeypatch.setattr(run_mod, "_resolve_bullets", lambda *a, **k: dict(colliding))
    monkeypatch.setattr(run_mod.compose, "inject_verbatim",
                        lambda *a, **k: {"__verbatim__/V/0": "Engineered exact words."})
    monkeypatch.setattr(run_mod.compose, "reverb", lambda *a, **k: "")   # force backstop, no LLM

    captured = {}
    real = run_mod.compose.dedupe_leading_verbs

    def spy(bullets, gm, jd, *, reserved=frozenset()):
        captured["reserved"] = set(reserved)
        captured["in"] = dict(bullets)
        out = real(bullets, gm, jd, reserved=reserved)
        captured["out"] = dict(out)
        return out

    monkeypatch.setattr(run_mod.compose, "dedupe_leading_verbs", spy)
    run_mod.tailor(_JOB)
    assert "engineered" in captured["reserved"]          # the verbatim opener is reserved
    assert captured["in"] == colliding                   # only the tailored bullets are deduped
    verbs = [run_mod.compose.leading_verb(t) for t in captured["out"].values()]
    assert verbs[0] == "built" and verbs[1] != "built"   # first kept, second made distinct
    assert "engineered" not in verbs                     # never collides with the reserved opener


def test_ats_report_false_skips_write_report(offline_tailor):
    run_mod.tailor(_JOB, ats_report=False)
    assert offline_tailor["ats"] == 0


def test_cover_letter_true_generates_cover(offline_tailor):
    run_mod.tailor(_JOB, cover_letter=True)
    assert offline_tailor["cover"] == 1


def test_cover_letter_ships_the_tex_and_no_txt(offline_tailor):
    # The .tex is the editable source (a manual fix recompiles it); the plain-text
    # export moved into apply.md, so no .txt is left in the folder.
    out = run_mod.tailor(_JOB, cover_letter=True)
    tex = out / "cover.tex"
    assert tex.exists()
    assert "documentclass" in tex.read_text(encoding="utf-8")
    assert list(out.glob("*_Cover_Letter.txt")) == []
    assert not (out / "cover.txt").exists()


def test_cover_letter_text_is_handed_to_apply_md(offline_tailor):
    run_mod.tailor(_JOB, cover_letter=True)
    assert offline_tailor["cover_body"] == "TXT:cover body:BigCo"


def test_no_cover_letter_hands_apply_md_no_cover_body(offline_tailor):
    run_mod.tailor(_JOB)
    assert not offline_tailor["cover_body"]


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


# --- SP3: degraded runs say so (warnings + tailor_report.txt) -----------------
#
# The stated pain with this pipeline was that failures surface late or not at all.
# Two holes fed it: enforce_one_page returning ok=True on a two-page PDF (the page
# count was computed every iteration and then discarded), and seven advisory `except`
# blocks that only called log() — a transient Qt status line that is gone by the next
# message. These tests pin the fix: a run that degrades emits warnings and leaves a
# durable record in the output folder, while still producing every artifact it
# produced before.

def _report(out: Path) -> str:
    return (out / run_mod.REPORT_NAME).read_text(encoding="utf-8")


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def _enforce_returning_pages(pages: int, pdf: Path):
    """A fake enforce_one_page that reports a real page count on its CompileResult."""
    def fake(sel, bullets, skills, tex_path, tmp_, jd, on_status=None, keep_projects=None):
        Path(tex_path).write_text("\\resumeItem{did a thing}", encoding="utf-8")
        return CompileResult(True, pdf, "", None, pages), dict(bullets), "TEX"
    return fake


def test_over_length_resume_warns_and_still_ships_the_pdf(offline_tailor, monkeypatch,
                                                          tmp_path):
    """The silent two-page PDF. tailor() used to check only `result.ok`, copy the PDF
    out and report success; now it compares CompileResult.pages against
    config.PAGE_LIMIT. The PDF still ships (a long résumé beats no résumé), the run
    just stops claiming it was clean."""
    pdf = tmp_path / "two_pages.pdf"
    pdf.write_bytes(b"%PDF-1.4 two")
    monkeypatch.setattr(run_mod, "enforce_one_page", _enforce_returning_pages(2, pdf))
    warns: list[str] = []
    out = run_mod.tailor(_JOB, on_warning=warns.append)
    assert [w for w in warns if w.startswith("page limit:") and "2 pages" in w]
    assert (out / "resume.pdf").exists()          # the artifact is still produced
    assert "page limit:" in _report(out)


def test_one_page_result_produces_no_warning(offline_tailor, monkeypatch, tmp_path):
    pdf = tmp_path / "one_page.pdf"
    pdf.write_bytes(b"%PDF-1.4 one")
    monkeypatch.setattr(run_mod, "enforce_one_page", _enforce_returning_pages(1, pdf))
    warns: list[str] = []
    out = run_mod.tailor(_JOB, on_warning=warns.append)
    assert warns == []
    text = _report(out)
    assert "pages   : 1 (limit 1)" in text
    assert "warnings (0)" in text and "  none" in text


def test_a_result_without_pages_is_not_read_as_overflow(offline_tailor):
    """The fixture's own fake_enforce predates the field and returns a result with no
    `pages` at all — every such stub in the suite must keep meaning "never measured",
    never "overflowed"."""
    warns: list[str] = []
    out = run_mod.tailor(_JOB, on_warning=warns.append)
    assert warns == []
    assert "pages   : not measured" in _report(out)


_ADVISORY_CASES = [
    ("ats", {},
     lambda mp: mp.setattr(run_mod.ats, "write_report", _boom),
     "advisory: ATS check skipped (boom)"),
    ("research", {"cover_letter": True},
     lambda mp: mp.setattr(run_mod.research, "company_blurb", _boom),
     "advisory: company research unavailable (boom)"),
    ("cover body", {"cover_letter": True},
     lambda mp: mp.setattr(run_mod.coverletter, "generate_body", _boom),
     "advisory: cover letter skipped (boom)"),
    ("cover compile", {"cover_letter": True},
     lambda mp: mp.setattr(
         run_mod.coverletter, "render_cover_letter",
         lambda body, company, tex_path, work_dir: (
             types.SimpleNamespace(ok=False, pdf_path=None, error="latex died"), "")),
     "advisory: cover letter compile failed (latex died)"),
    ("cover text", {"cover_letter": True},
     lambda mp: mp.setattr(run_mod.coverletter, "cover_letter_text", _boom),
     "advisory: cover letter text skipped (boom)"),
    ("prep", {"prep_sheet": True},
     lambda mp: mp.setattr(prep_mod, "generate_prep_sheet", _boom),
     "advisory: interview prep skipped (boom)"),
    ("apply.md", {},
     lambda mp: mp.setattr(run_mod.apply_data, "write", _boom),
     "advisory: apply sheet skipped (boom)"),
]


@pytest.mark.parametrize("opts,setup,expected", [c[1:] for c in _ADVISORY_CASES],
                         ids=[c[0] for c in _ADVISORY_CASES])
def test_each_advisory_failure_reaches_the_collector_and_the_report(
        offline_tailor, monkeypatch, opts, setup, expected):
    """All seven advisory swallows. Each `except` still logs exactly as before; it now
    ALSO records a warning, so a half-worked run leaves evidence that outlives the
    status bar."""
    setup(monkeypatch)
    warns: list[str] = []
    out = run_mod.tailor(_JOB, on_warning=warns.append, **opts)
    assert expected in warns
    assert expected in _report(out)


def test_grounding_gate_findings_reach_the_warning_collector(offline_tailor, monkeypatch):
    """The gate's {gkey: unseen_tokens} return was discarded at all four call sites, and
    the gate is silent on grounded text — so a run it had to salvage rendered identically
    to one it never touched. Now every reverted or dropped bullet becomes a warning
    naming the pass that caused it, the bullet, and the tokens with no trace in its own
    atoms."""
    monkeypatch.setattr(run_mod, "_resolve_bullets",
                        lambda *a, **k: {"a": "Kept the thing.", "b": "Broke the thing."})
    real = run_mod.verify.enforce_grounded
    calls: list[bool] = []

    def gate(sel, bullets, *, fallback=None, log=None):
        calls.append(fallback is None)
        if len(calls) == 2:            # the first fallback-bearing pass: verb dedupe
            del bullets["b"]           # no grounded fallback -> dropped outright
            return {"a": ["MIT"], "b": ["PhD"]}   # 'a' survives -> reverted
        return real(sel, bullets, fallback=fallback, log=log)

    monkeypatch.setattr(run_mod.verify, "enforce_grounded", gate)
    warns: list[str] = []
    out = run_mod.tailor(_JOB, on_warning=warns.append)
    assert "grounding: [verb dedupe] reverted bullet 'a' (ungrounded: MIT)" in warns
    assert "grounding: [verb dedupe] dropped bullet 'b' (ungrounded: PhD)" in warns
    assert "dropped bullet 'b'" in _report(out)


def test_run_report_lists_the_stages_that_ran(offline_tailor):
    """The report's stage list is keyed off Pass.name, so the pipeline itself names the
    stages instead of a hand-kept second copy of the order."""
    out = run_mod.tailor(_JOB)
    text = _report(out)
    for stage in ("select", "block briefs", "rephrase", "verb dedupe", "verbatim + trim",
                  "style gate", "skills", "render + compile", "ats report", "apply sheet"):
        assert f"\n  {stage}\n" in text, stage
    assert "BigCo" in text and "Engineer" in text        # the job it was written for


def test_report_is_written_without_an_on_warning_callback(offline_tailor):
    """on_warning defaults to None (no churn at any existing call site); the durable
    record is written either way."""
    out = run_mod.tailor(_JOB)
    assert (out / run_mod.REPORT_NAME).exists()


def test_a_failed_report_write_is_logged_not_raised(offline_tailor, monkeypatch):
    """Writing the record of failures must never itself sink a run that produced a PDF,
    and it must not fail silently either."""
    monkeypatch.setattr(run_mod, "_report_text", _boom)
    msgs: list[str] = []
    out = run_mod.tailor(_JOB, on_status=msgs.append)
    assert (out / "resume.pdf").exists()
    assert any(f"{run_mod.REPORT_NAME} not written" in m for m in msgs)


def test_a_broken_warning_collector_never_sinks_the_run(offline_tailor, monkeypatch):
    """on_warning is somebody else's code. A raising collector degrades the live stream,
    not the run, and the report still records the warning."""
    monkeypatch.setattr(run_mod.ats, "write_report", _boom)
    out = run_mod.tailor(_JOB, on_warning=_boom)
    assert "advisory: ATS check skipped (boom)" in _report(out)
