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
from test_jev_score import (JOB2_MD, RESUME, RefusesStage2For, RecordingPool, ScriptedJudge,
                            _jobs_df)


def _resp(text):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
    )


# --- prompt pins -------------------------------------------------------------

def test_writer_system_flags_the_job_description_and_findings_as_untrusted():
    low = sj.WRITER_SYSTEM.lower()
    assert "job description" in low and "requirement lines" in low and "findings" in low
    assert "untrusted data" in low
    assert "ignore any instructions" in low


def test_writer_template_bans_the_same_gaps_as_stage_two():
    low = sj.WRITER_TEMPLATE_RESUME.format(
        resume="RESUME", today="TODAY",
        **sj.candidate_prompt_vars(sj.candidate_profile())).lower()
    for kw in ("relocat", "on-site", "hybrid", "remote", "time zone",
              "work authorization", "sponsorship", "career path", "business background"):
        assert kw in low, kw


def test_writer_prompts_are_free_of_em_dashes():
    for text in (sj.WRITER_SYSTEM, sj.WRITER_TEMPLATE_RESUME, sj.WRITER_TEMPLATE_JOB):
        assert "—" not in text


def test_writer_prompt_still_asks_for_no_em_dashes_alongside_the_code_backstop():
    # The prompt ban and _strip_em_dashes are two layers, same as the tailor's
    # style gate plus its own mechanical strip; this pins the prompt layer so
    # a later edit cannot drop it on the assumption the code backstop covers it.
    assert "no em dashes and no hype" in sj.WRITER_TEMPLATE_RESUME


def test_writer_templates_format_with_resume_today_findings_and_job():
    resume_half = sj.WRITER_TEMPLATE_RESUME.format(
        resume="RESUME-TEXT", today="Sep 28, 2026",
        **sj.candidate_prompt_vars(sj.candidate_profile()))
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


def test_writer_findings_omits_the_parenthetical_for_a_score_with_no_label():
    """M2: SCORE_LABELS[score] raised KeyError for a score outside 1-5. .get()
    leaves the parenthetical out instead of crashing."""
    row = {"deep_score": 8, "recommendation": "apply", "findings": _findings()}
    block = sj.writer_findings(7, row)
    assert block.startswith("Fit score: 7 of 5\n")
    assert "(" not in block.splitlines()[0]


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


def test_write_notes_strips_an_em_dash_with_spaces_from_the_reason():
    pool = FakeWriterPool(_ok_json(reason="Good fit — strong skills match."))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out["reason"] == "Good fit, strong skills match."


def test_write_notes_strips_a_bare_em_dash_from_a_strength():
    pool = FakeWriterPool(_ok_json(strengths=["Python—SQL"]))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out["strengths"] == "Python, SQL"


def test_write_notes_strips_a_bare_em_dash_from_a_gap():
    pool = FakeWriterPool(_ok_json(gaps=["Kubernetes—Docker"]))
    out = asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    assert out["gaps"] == "Kubernetes, Docker"


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


# --- M9: one failure report per reason, no message or job text ----------------

def test_write_notes_reports_the_exception_class_once_per_class(monkeypatch, capsys):
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())
    pool = FakeWriterPool(exc=RuntimeError("kaboom, quotes the resume and the job"))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J2", "job md", "block"))
    out = capsys.readouterr().out
    assert out.count("Jev writer: call failed (RuntimeError); keeping Jev's code text.") == 1
    assert "kaboom" not in out and "resume" not in out and "job" not in out


def test_write_notes_reports_a_different_exception_class_separately(monkeypatch, capsys):
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())
    asyncio.run(sj.write_notes(FakeWriterPool(exc=RuntimeError("x")), asyncio.Semaphore(1),
                               RESUME, "J1", "job md", "block"))
    asyncio.run(sj.write_notes(FakeWriterPool(exc=ValueError("y")), asyncio.Semaphore(1),
                               RESUME, "J2", "job md", "block"))
    out = capsys.readouterr().out
    assert "Jev writer: call failed (RuntimeError); keeping Jev's code text." in out
    assert "Jev writer: call failed (ValueError); keeping Jev's code text." in out


def test_write_notes_reports_bad_json_once(monkeypatch, capsys):
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())
    asyncio.run(sj.write_notes(FakeWriterPool("not json at all"), asyncio.Semaphore(1),
                               RESUME, "J1", "job md", "block"))
    asyncio.run(sj.write_notes(FakeWriterPool("still not json"), asyncio.Semaphore(1),
                               RESUME, "J2", "job md", "block"))
    out = capsys.readouterr().out
    assert out.count("Jev writer: bad JSON; keeping Jev's code text.") == 1


def test_write_notes_reports_a_blank_reason_once(monkeypatch, capsys):
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())
    pool = FakeWriterPool(_ok_json(reason="   "))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J2", "job md", "block"))
    out = capsys.readouterr().out
    assert out.count("Jev writer: a blank reason; keeping Jev's code text.") == 1


def test_write_notes_reports_no_strengths_once(monkeypatch, capsys):
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())
    pool = FakeWriterPool(_ok_json(strengths=[]))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J1", "job md", "block"))
    asyncio.run(sj.write_notes(pool, asyncio.Semaphore(1), RESUME, "J2", "job md", "block"))
    out = capsys.readouterr().out
    assert out.count("Jev writer: no strengths; keeping Jev's code text.") == 1


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


def test_a_job_with_no_matching_stage_one_row_gets_no_writer_call():
    """A code-review guard: `s1_row["score"].iloc[0]` used to have no guard for
    an empty `s1_row`. A blank job_posting_id reproduces it -- `merge` and
    `isin` line NaN keys up with each other, but `==` never does, so the
    writer hook's own `s1_df["job_posting_id"] == job_id` lookup comes back
    empty even though the job's stage 1 row is right there. The writer is
    skipped, Jev's own composed reason stays, and it counts as kept code text."""
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    df = pd.DataFrame({
        "job_posting_id": [float("nan")],
        "job_description_md": [f"JOB-A\n{JOB2_MD}"],
        "filtered_out": [False],
    })
    merged = asyncio.run(sj.run_scoring(pool, RESUME, df, jev_run=run))
    assert pool.calls == []
    assert merged["reason"].iloc[0] == (
        "Strong match: no experience bar, and the skills, tools and field line up.")
    assert run.writer == {"written": 0, "kept": 1}


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


# --- M2: a stage 1 score with no label never crashes the gather ----------------

class RefusesStage1(ScriptedJudge):
    """Raises for a stage 1 request (no `deep_fit` among the questions), so
    stage 1 falls back to the LLM path; answers stage 2 normally."""

    def judge(self, state, questions):
        if "deep_fit" not in questions:
            raise RuntimeError("no stage 1 answer for this one")
        return super().judge(state, questions)


class Stage1FallbackAndWriterPool:
    """The one LLM provider seen by both the stage 1 fallback (an odd score of
    7, out of Jev's own 1-5 range) and the writer's own call: both ride
    STAGE1_MODELS, told apart by which schema the request asks for."""

    def __init__(self):
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if config.response_schema == sj.STAGE1_SCHEMA:
            return _resp(json.dumps({"score": 7, "reason": "odd fallback score"}))
        return _resp(_ok_json(reason="Writer reason.", strengths=["Writer strength"]))


def test_a_stage_one_score_with_no_label_does_not_crash_run_scoring():
    pool = Stage1FallbackAndWriterPool()
    run = sj.JevRun(RefusesStage1())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A"), jev_run=run)
                        ).set_index("job_posting_id")
    assert merged.loc["job-a", "score"] == 7
    assert merged.loc["job-a", "reason"] == "Writer reason."   # the writer still ran
    assert run.writer == {"written": 1, "kept": 0}


# --- I2: the writer stops after WRITER_FAIL_LIMIT failures in a row -----------

def test_three_writer_failures_in_a_row_stop_it_for_the_rest_of_the_run(monkeypatch, capsys):
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 1)   # deterministic order, see test_jev_score.py
    tags = [f"JOB-{c}" for c in "ABCDE"]
    pool = WriterPool(exc=RuntimeError("boom"))
    run = sj.JevRun(ScriptedJudge())
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df(*tags), jev_run=run))
    assert len(pool.calls) == sj.WRITER_FAIL_LIMIT      # the later jobs made no call at all
    assert run.writer == {"written": 0, "kept": len(tags)}
    out = capsys.readouterr().out
    assert out.count("Jev writer: stopped after 3 failures in a row; the remaining jobs keep "
                     "Jev's code text.") == 1


class PerJobOutcomePool:
    """The writer's LLM provider, keyed by which job tag is in the contents:
    success text, or the given exception, per `outcomes`."""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        tag = next(t for t in self.outcomes if t in contents)
        outcome = self.outcomes[tag]
        if isinstance(outcome, Exception):
            raise outcome
        return _resp(_ok_json(reason=f"{tag} reason.", strengths=[f"{tag} strength"]))


def test_a_success_between_failures_resets_the_streak(monkeypatch, capsys):
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 1)
    tags = ["JOB-A", "JOB-B", "JOB-C", "JOB-D", "JOB-E", "JOB-F"]
    outcomes = {"JOB-A": RuntimeError("x"), "JOB-B": RuntimeError("x"), "JOB-C": "ok",
                "JOB-D": RuntimeError("x"), "JOB-E": RuntimeError("x"), "JOB-F": RuntimeError("x")}
    pool = PerJobOutcomePool(outcomes)
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df(*tags), jev_run=run)
                        ).set_index("job_posting_id")
    assert len(pool.calls) == 6      # C's success reset the streak, so F still got a call
    assert run.writer == {"written": 1, "kept": 5}
    assert merged.loc["job-c", "reason"] == "JOB-C reason."
    out = capsys.readouterr().out
    assert out.count("Jev writer: stopped after 3 failures in a row; the remaining jobs keep "
                     "Jev's code text.") == 1


# --- M8: each job in a multi-job run keeps its own writer outcome -------------

def test_run_scoring_writer_gives_each_job_its_own_notes_and_keeps_a_failed_jobs_code_text():
    pool = PerJobOutcomePool({"JOB-A": "ok", "JOB-B": RuntimeError("boom")})
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A", "JOB-B"), jev_run=run)
                        ).set_index("job_posting_id")
    assert merged.loc["job-a", "reason"] == "JOB-A reason."
    assert merged.loc["job-a", "strengths"] == "JOB-A strength"
    assert merged.loc["job-b", "reason"] == (
        "Strong match: no experience bar, and the skills, tools and field line up.")
    assert merged.loc["job-b", "strengths"].startswith("Python and SQL")
    assert run.writer == {"written": 1, "kept": 1}


# --- the latch's edges: a local findings error, two passes, overlapping calls --

def test_a_findings_block_error_keeps_code_text_without_feeding_the_streak(monkeypatch, capsys):
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 1)
    monkeypatch.setattr(sj, "_WRITER_WARNED", set())

    def broken(score, row):
        raise ValueError("bad findings")

    monkeypatch.setattr(sj, "writer_findings", broken)
    tags = [f"JOB-{c}" for c in "ABCD"]
    pool = WriterPool()
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df(*tags), jev_run=run)
                        ).set_index("job_posting_id")
    assert pool.calls == []                  # no block, no call
    assert run.writer == {"written": 0, "kept": len(tags)}
    assert not run.writer_stopped            # a local error is no provider outage
    assert merged.loc["job-a", "strengths"].startswith("Python and SQL")
    out = capsys.readouterr().out
    assert out.count("Jev writer: findings error (ValueError); keeping Jev's code text.") == 1
    assert "stopped after" not in out


def test_the_writer_latch_holds_across_two_run_scoring_passes_on_one_jev_run(monkeypatch):
    # The fresh pass and the rescore pass share one JevRun, so a latch tripped
    # in the first pass keeps the second pass from calling the writer at all.
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 1)
    run = sj.JevRun(ScriptedJudge())
    failing = WriterPool(exc=RuntimeError("boom"))
    asyncio.run(sj.run_scoring(failing, RESUME, _jobs_df("JOB-A", "JOB-B", "JOB-C"), jev_run=run))
    assert run.writer_stopped
    healthy = WriterPool()
    asyncio.run(sj.run_scoring(healthy, RESUME, _jobs_df("JOB-D", "JOB-E"), jev_run=run))
    assert healthy.calls == []
    assert run.writer == {"written": 0, "kept": 5}


class SlowFailingPool:
    """A writer provider whose calls overlap: each one yields to the event loop
    before it fails, so several are in flight when the latch trips."""

    def __init__(self):
        self.calls = 0

    async def generate(self, *, model, contents, config):
        self.calls += 1
        await asyncio.sleep(0.01)
        raise RuntimeError("usage limit")


def test_overlapping_writer_failures_count_every_job_and_stop_once(capsys):
    tags = [f"JOB-{c}" for c in "ABCDEFGH"]
    pool = SlowFailingPool()
    run = sj.JevRun(ScriptedJudge())
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df(*tags), jev_run=run))
    assert run.writer == {"written": 0, "kept": len(tags)}
    assert run.writer_stopped
    assert sj.WRITER_FAIL_LIMIT <= pool.calls <= len(tags)
    out = capsys.readouterr().out
    assert out.count("Jev writer: stopped after 3 failures in a row") == 1


def test_jev_run_note_writer_result_owns_the_streak_and_the_latch(capsys):
    """run_scoring reports each writer call's outcome through
    `JevRun.note_writer_result`; the streak and the latch stay inside JevRun."""
    run = sj.JevRun()
    for _ in range(sj.WRITER_FAIL_LIMIT - 1):
        run.note_writer_result(False)
    run.note_writer_result(True)                  # a success resets the streak
    assert not run.writer_stopped
    for _ in range(sj.WRITER_FAIL_LIMIT):
        run.note_writer_result(False)
    assert run.writer_stopped
    run.note_writer_result(False)                 # latched once, said once
    assert run.writer == {"written": 1, "kept": 2 * sj.WRITER_FAIL_LIMIT}
    assert capsys.readouterr().out.count("Jev writer: stopped after") == 1
