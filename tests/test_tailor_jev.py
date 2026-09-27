"""The tailor's Jev steps (TL-1 to TL-3) as `run.tailor()` meets them.

With Jev off the tailor must make exactly the LLM calls it made before cycle 19,
with byte-identical prompts. `test_jev_off_prompts_match_the_recording` pins that:
the prompts of the three stages the Jev steps sit beside (`select`, the
`lead_with_overview` ordering call and `compress_skills`' fallback call) and the
order and tier of every call a golden run makes were recorded from the engine
before the Jev wiring landed, into `tests/fixtures/tailor_jev_off_prompts.json`.
A prompt change made on purpose re-records the file (set TAILOR_JEV_OFF_PROMPTS_RECORD=1
for one run) and shows the diff in review; a change nobody meant fails here.

The runs reuse the golden module's pinned engine (`test_tailor_golden.pinned_engine`),
so no model is ever reached: its stub raises on any prompt it does not know.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_data, compose, output, render, skills  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402
from resume_tailor.compile import CompileResult  # noqa: E402

import test_tailor_golden as golden  # noqa: E402 - sibling test module, no pkg import

# The golden module's fixtures, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

PROMPTS = REPO / "tests" / "fixtures" / "tailor_jev_off_prompts.json"
RECORD_ENV = "TAILOR_JEV_OFF_PROMPTS_RECORD"
# The stages whose prompts are pinned word for word: the ones a Jev step replaces
# or trims when Jev is on.
PINNED_STAGES = ("select", "lead_with_overview")

# compress_skills' fallback answer: any pool-backed lines will do, the prompt is
# what is recorded.
_FALLBACK_SKILLS = {"Languages": "Python, SQL", "Frameworks": "FastAPI",
                    "Developer Tools": "Git", "Libraries": "pandas"}


def _recording(stages, calls):
    """The golden stub, recording every call's prompts, tier and options."""
    stub = golden._make_stub(stages)

    def _call(system, user, tier, **kw):
        out = stub(system, user, tier, **kw)
        calls.append({"stage": stages[-1], "system": system, "user": user,
                      "tier": tier, "kw": kw})
        return out
    return _call


def _run_tailor(monkeypatch, tmp_path, job=None):
    """`run.tailor()` over the golden job, stubbed at the render seam exactly as
    `test_run_tailor_matches_the_golden` stubs it. Returns what reached the page."""
    captured: dict = {}

    def _fake_enforce(sel, bullets, skill_lines, tex_path, work_dir, jd="",
                      on_status=None, keep_projects=None):
        tex = render.render(sel, bullets, skill_lines)
        Path(tex_path).write_text(tex, encoding="utf-8")
        pdf = Path(work_dir) / "golden.pdf"
        pdf.write_bytes(b"%PDF-1.4 golden stub\n")
        captured.update(sel=sel, bullets=dict(bullets), skill_lines=list(skill_lines),
                        tex=tex)
        return CompileResult(True, pdf, ""), dict(bullets), tex

    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(rt_run, "pdflatex_available", lambda: True)
    monkeypatch.setattr(rt_run, "enforce_one_page", _fake_enforce)
    monkeypatch.setattr(output, "resolve_dir", lambda *a, **k: out)
    monkeypatch.setattr(apply_data, "write", lambda *a, **k: None)
    captured["out"] = rt_run.tailor(job or golden._JOB, ats_report=False)
    return captured


def _lines(text):
    return text.split("\n")


def _record_jev_off(monkeypatch, tmp_path):
    """Everything the recording pins, from one golden run plus one fallback call."""
    stages: list = []
    calls: list = []
    golden._install_stub(monkeypatch, _recording(stages, calls))
    _run_tailor(monkeypatch, tmp_path)

    fallback: dict = {}

    def _fallback_call(system, user, tier, **kw):
        fallback.update(system=system, user=user, tier=tier, kw=kw)
        return dict(_FALLBACK_SKILLS)

    monkeypatch.setattr(skills, "call", _fallback_call)
    compose.compress_skills(golden._JD, golden._JOB["job_title"],
                            {"skills": {}, "skill_focus": "data_analytics"})

    prompts = {c["stage"]: {"system": _lines(c["system"]), "user": _lines(c["user"])}
               for c in calls if c["stage"] in PINNED_STAGES}
    prompts["skills_fallback"] = {"system": _lines(fallback["system"]),
                                  "user": _lines(fallback["user"])}
    return {
        "calls": [{"stage": c["stage"], "tier": c["tier"], "kw": c["kw"]} for c in calls],
        "prompts": prompts,
        "skills_fallback_call": {"tier": fallback["tier"], "kw": fallback["kw"]},
    }


def test_jev_off_prompts_match_the_recording(pinned_engine, stub_template_head,
                                             tmp_path, monkeypatch):
    """Jev off (the suite's default: no TypeSafe key) makes exactly the recorded
    calls, and the stages beside the Jev steps send the recorded prompts."""
    got = _record_jev_off(monkeypatch, tmp_path)
    if os.environ.get(RECORD_ENV) == "1":
        PROMPTS.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
        pytest.skip(f"recorded {PROMPTS.name}; run again without {RECORD_ENV}")
    want = json.loads(PROMPTS.read_text(encoding="utf-8"))
    assert [c["stage"] for c in got["calls"]] == [c["stage"] for c in want["calls"]]
    assert got["calls"] == want["calls"]
    for stage in (*PINNED_STAGES, "skills_fallback"):
        assert got["prompts"][stage]["system"] == want["prompts"][stage]["system"], stage
        assert got["prompts"][stage]["user"] == want["prompts"][stage]["user"], stage
    assert got["skills_fallback_call"] == want["skills_fallback_call"]
    assert got == want
