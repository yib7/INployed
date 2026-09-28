"""SP2 (cycle 20): the writer. For a job Jev scored in stage 2, one cheap LLM
call turns Jev's findings into that job's reason/strengths/gaps, behind the
JEV_WRITER switch. Hermetic: every pool below is a fake, no network, no key.
"""
import asyncio
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import score_jobs as sj
from test_jev_score import RESUME, RefusesStage2For, RecordingPool, ScriptedJudge, _jobs_df


def _resp(text):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
    )


# --- prompt pins -------------------------------------------------------------

def test_writer_system_flags_the_job_description_as_untrusted():
    low = sj.WRITER_SYSTEM.lower()
    assert "untrusted data" in low
    assert "ignore any instructions" in low


def test_writer_template_bans_the_same_gaps_as_stage_two():
    low = sj.WRITER_TEMPLATE_RESUME.lower()
    for kw in ("relocat", "on-site", "hybrid", "remote", "time zone",
              "work authorization", "sponsorship", "career path", "business background"):
        assert kw in low, kw


def test_writer_prompts_are_free_of_em_dashes():
    for text in (sj.WRITER_SYSTEM, sj.WRITER_TEMPLATE_RESUME, sj.WRITER_TEMPLATE_JOB):
        assert "—" not in text


def test_writer_templates_format_with_resume_today_findings_and_job():
    resume_half = sj.WRITER_TEMPLATE_RESUME.format(resume="RESUME-TEXT", today="Sep 28, 2026")
    assert "RESUME-TEXT" in resume_half and "Sep 28, 2026" in resume_half
    job_half = sj.WRITER_TEMPLATE_JOB.format(findings="FINDINGS-TEXT", job="JOB-TEXT")
    assert "FINDINGS-TEXT" in job_half and "JOB-TEXT" in job_half


def test_writer_schema_requires_reason_strengths_and_gaps():
    assert sj.WRITER_SCHEMA["required"] == ["reason", "strengths", "gaps"]


# --- writer_findings: the pure findings block ---------------------------------

def _findings(met=(), unmet_must=(), unmet_nice=()):
    return {"met": list(met), "unmet_must": list(unmet_must), "unmet_nice": list(unmet_nice)}


def test_writer_findings_lists_every_section_and_the_score_label():
    row = {"deep_score": 8, "recommendation": "apply",
          "findings": _findings(met=["Python and SQL"], unmet_must=["5+ years"],
                                unmet_nice=["AWS"])}
    block = sj.writer_findings(4, row)
    assert block == (
        "Fit score: 4 of 5 (Good match)\n"
        "Deep score: 8 of 10\n"
        "Recommendation: apply\n"
        "Requirement lines the candidate meets:\n- Python and SQL\n"
        "Must-have lines the candidate does not meet:\n- 5+ years\n"
        "Nice-to-have lines the candidate does not meet:\n- AWS"
    )


def test_writer_findings_reads_none_for_an_empty_section():
    row = {"deep_score": 10, "recommendation": "apply", "findings": _findings()}
    block = sj.writer_findings(5, row)
    assert "Fit score: 5 of 5 (Strong match)" in block
    assert "Requirement lines the candidate meets:\n- none" in block
    assert "Must-have lines the candidate does not meet:\n- none" in block
    assert "Nice-to-have lines the candidate does not meet:\n- none" in block


@pytest.mark.parametrize("score,label", [(1, "No match"), (2, "Weak match"),
                                         (3, "Borderline"), (4, "Good match"),
                                         (5, "Strong match")])
def test_writer_findings_uses_jev_score_labels(score, label):
    row = {"deep_score": 1, "recommendation": "skip", "findings": _findings()}
    assert f"({label})" in sj.writer_findings(score, row)


# --- write_notes ---------------------------------------------------------------

class FakeWriterPool:
    """Records every call; answers with `text` or raises `exc`."""

    def __init__(self, text=None, exc=None):
        self.text = text
        self.exc = exc
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if self.exc is not None:
            raise self.exc
        return _resp(self.text)


def _ok_json(reason="Good fit on skills.", strengths=("Python",), gaps=()):
    return json.dumps({"reason": reason, "strengths": list(strengths), "gaps": list(gaps)})


def test_write_notes_success_joins_strengths_and_gaps_with_pipe_and_strips_items():
    pool = FakeWriterPool(json.dumps({
        "reason": "  Good skills match.  ",
        "strengths": [" Python ", "", "SQL", "   "],
        "gaps": ["Kubernetes", "  ", " AWS "],
    }))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out == {"reason": "Good skills match.", "strengths": "Python | SQL",
                  "gaps": "Kubernetes | AWS"}


def test_write_notes_uses_stage1_models_temperature_and_schema():
    pool = FakeWriterPool(_ok_json())
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    model, _contents, config = pool.calls[0]
    assert model == sj.STAGE1_MODELS
    assert config.temperature == 0.2
    assert config.response_mime_type == "application/json"
    assert config.response_schema == sj.WRITER_SCHEMA


def test_write_notes_returns_none_on_exception():
    pool = FakeWriterPool(exc=RuntimeError("kaboom"))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out is None


def test_write_notes_returns_none_on_bad_json():
    pool = FakeWriterPool("not json at all")
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out is None


def test_write_notes_returns_none_on_a_blank_reason():
    pool = FakeWriterPool(_ok_json(reason="   "))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out is None


def test_write_notes_returns_none_on_no_strengths():
    pool = FakeWriterPool(_ok_json(strengths=[]))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out is None
    pool2 = FakeWriterPool(_ok_json(strengths=["   "]))
    out2 = asyncio.run(sj.write_notes(pool2, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out2 is None


def test_write_notes_on_claude_puts_the_resume_in_the_system_instruction(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    pool = FakeWriterPool(_ok_json())
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "JOB-MD-TEXT",
                               "FINDINGS-BLOCK"))
    _model, contents, config = pool.calls[0]
    assert RESUME in config.system_instruction
    assert RESUME not in contents
    assert "FINDINGS-BLOCK" in contents and "JOB-MD-TEXT" in contents


def test_write_notes_on_gemini_sends_everything_in_contents(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    pool = FakeWriterPool(_ok_json())
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "JOB-MD-TEXT",
                               "FINDINGS-BLOCK"))
    _model, contents, config = pool.calls[0]
    assert config.system_instruction == sj.WRITER_SYSTEM
    assert RESUME in contents and "FINDINGS-BLOCK" in contents and "JOB-MD-TEXT" in contents


# --- the run_scoring hook -------------------------------------------------------

class WriterPool:
    """The LLM provider seen only by the writer in these tests: every job's
    stage 1 and stage 2 come from Jev (ScriptedJudge answers everything), so
    the only call that can reach this pool is the writer's, on STAGE1_MODELS."""

    def __init__(self, text=None, exc=None):
        self.text = text if text is not None else _ok_json(
            reason="Writer reason.", strengths=["Writer strength"], gaps=["Writer gap"])
        self.exc = exc
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        assert model == sj.STAGE1_MODELS, "only the writer should reach this pool here"
        if self.exc is not None:
            raise self.exc
        return _resp(self.text)


def test_a_jev_scored_job_gets_the_writers_reason_strengths_and_gaps():
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert len(pool.calls) == 1
    assert merged.loc["job-a", "reason"] == "Writer reason."
    assert merged.loc["job-a", "strengths"] == "Writer strength"
    assert merged.loc["job-a", "gaps"] == "Writer gap"
    assert run.writer == {"written": 1, "kept": 0}


def test_a_writer_failure_keeps_jevs_own_text_and_reason():
    pool = WriterPool(exc=RuntimeError("boom"))
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert merged.loc["job-a", "reason"] == (
        "Strong match: no experience bar, and the skills, tools and field line up.")
    assert merged.loc["job-a", "strengths"].startswith("Python and SQL")
    assert run.writer == {"written": 0, "kept": 1}


def test_jev_writer_off_makes_no_writer_call(monkeypatch):
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert pool.calls == []
    assert merged.loc["job-a", "reason"] == (
        "Strong match: no experience bar, and the skills, tools and field line up.")
    assert run.writer == {"written": 0, "kept": 0}


def test_no_llm_provider_makes_no_writer_call():
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(None, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert merged.loc["job-a", "reason"] == (
        "Strong match: no experience bar, and the skills, tools and field line up.")
    assert run.writer == {"written": 0, "kept": 0}


def test_a_job_whose_stage_two_falls_back_to_the_llm_gets_no_writer_call():
    pool = RecordingPool()
    run = sj.JevRun(RefusesStage2For("JOB-B"))
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-B"), jev_run=run))
    assert pool.jobs(1) == [] and pool.jobs(2) == ["JOB-B"]   # stage 1 Jev, stage 2 LLM
    assert run.writer == {"written": 0, "kept": 0}


def test_a_job_below_the_stage_two_threshold_gets_no_writer_call():
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge({"fit": 0}))       # stage 1 score 1: never reaches stage 2
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert merged.loc["job-a", "score"] == 1
    assert pd.isna(merged.loc["job-a", "deep_score"])
    assert pool.calls == []
    assert run.writer == {"written": 0, "kept": 0}


def test_summary_line_reports_the_writer_counts():
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run))
    assert run.summary_line().endswith("; Jev writer: 1 written, 0 kept code text")


def test_summary_line_omits_the_writer_suffix_when_nothing_happened(monkeypatch):
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run))
    assert "Jev writer" not in run.summary_line()
