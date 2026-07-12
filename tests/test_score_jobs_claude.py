"""Tests for the optional local Claude scoring provider (SP4).

Covers: the three new scoring_config.json keys, the pure `_active_scoring`
truth table (junk values must resolve "gemini" -- this pins the VM's
behavior, since the VM ships no scoring_config.json), `make_pool()`'s claude
branch + VM-safe fallback (missing CLI / ImportError -> printed warning,
never sys.exit), `score_stage1` against a fake claude-shaped pool, and the
cache-friendly prompt split (gemini bytes unchanged; claude system prompt
carries the resume and is job-independent).

Hermetic: a fake `claude_cli` module is injected into sys.modules for the
make_pool tests (removed in a finally block); no real CLI/network calls.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import score_jobs as sj  # noqa: E402


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "scoring_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _clear_env(monkeypatch):
    for k in (
        "SCORE_PROVIDER", "SCORE_STAGE1_MODEL", "SCORE_STAGE2_MODEL",
        "SCORE_STAGE1_MODEL_CLAUDE", "SCORE_STAGE2_MODEL_CLAUDE",
        "SCORE_STAGE1_CONCURRENCY", "SCORE_STAGE2_CONCURRENCY",
        "SCORE_STAGE2_THRESHOLD", "SCORE_MAX_PER_RUN", "SCORE_RESCORE_CAP",
        "SCORE_MIN_FILTER_YEARS", "SCORE_CLAUDE_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------
# New config keys: default / file / env precedence
# --------------------------------------------------------------------------

def test_new_keys_use_builtin_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    cfg = sj.load_scoring_config()
    assert cfg["provider"] == "gemini"
    assert cfg["stage1_model_claude"] == "claude-haiku-4-5"
    assert cfg["stage2_model_claude"] == "claude-sonnet-5"


def test_new_keys_file_override(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {
        "provider": "claude",
        "stage1_model_claude": "claude-opus-4-8",
        "stage2_model_claude": "claude-opus-4-8",
    })
    cfg = sj.load_scoring_config()
    assert cfg["provider"] == "claude"
    assert cfg["stage1_model_claude"] == "claude-opus-4-8"
    assert cfg["stage2_model_claude"] == "claude-opus-4-8"


def test_new_keys_env_wins_over_file(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"provider": "claude"})
    monkeypatch.setenv("SCORE_PROVIDER", "gemini")
    monkeypatch.setenv("SCORE_STAGE1_MODEL_CLAUDE", "claude-x")
    cfg = sj.load_scoring_config()
    assert cfg["provider"] == "gemini"  # env beats the file's "claude"
    assert cfg["stage1_model_claude"] == "claude-x"


# --------------------------------------------------------------------------
# _active_scoring truth table
# --------------------------------------------------------------------------

def test_active_scoring_default_is_gemini():
    cfg = {
        "provider": "gemini",
        "stage1_model": "gemini-3.1-flash-lite", "stage2_model": "gemini-3.5-flash",
        "stage1_model_claude": "claude-haiku-4-5", "stage2_model_claude": "claude-sonnet-5",
    }
    assert sj._active_scoring(cfg) == ("gemini", "gemini-3.1-flash-lite", "gemini-3.5-flash")


def test_active_scoring_claude_selects_claude_models():
    cfg = {
        "provider": "claude",
        "stage1_model": "gemini-3.1-flash-lite", "stage2_model": "gemini-3.5-flash",
        "stage1_model_claude": "claude-haiku-4-5", "stage2_model_claude": "claude-sonnet-5",
    }
    assert sj._active_scoring(cfg) == ("claude", "claude-haiku-4-5", "claude-sonnet-5")


def test_active_scoring_junk_values_pin_gemini():
    """Anything but exactly 'claude' (post strip/lower) resolves gemini -- this
    is what keeps the VM (no scoring_config.json => provider key absent =>
    cfg.get default "") safely on Gemini."""
    base = {
        "stage1_model": "gemini-3.1-flash-lite", "stage2_model": "gemini-3.5-flash",
        "stage1_model_claude": "claude-haiku-4-5", "stage2_model_claude": "claude-sonnet-5",
    }
    for junk in ("openai", "", None, "claudee", "claude2", "claude-cli", "gpt"):
        cfg = {**base, "provider": junk}
        provider, s1, s2 = sj._active_scoring(cfg)
        assert provider == "gemini", junk
        assert s1 == "gemini-3.1-flash-lite"
        assert s2 == "gemini-3.5-flash"


def test_active_scoring_claude_with_whitespace_and_case():
    base = {
        "stage1_model": "gemini-3.1-flash-lite", "stage2_model": "gemini-3.5-flash",
        "stage1_model_claude": "claude-haiku-4-5", "stage2_model_claude": "claude-sonnet-5",
    }
    for val in ("claude", " claude ", "Claude", "CLAUDE"):
        cfg = {**base, "provider": val}
        provider, s1, s2 = sj._active_scoring(cfg)
        assert provider == "claude", val
        assert s1 == "claude-haiku-4-5"
        assert s2 == "claude-sonnet-5"


def test_active_scoring_missing_provider_key_defaults_gemini():
    """cfg.get("provider", "") when the key is wholly absent (defensive; in
    practice load_scoring_config always fills it)."""
    cfg = {
        "stage1_model": "gemini-3.1-flash-lite", "stage2_model": "gemini-3.5-flash",
        "stage1_model_claude": "claude-haiku-4-5", "stage2_model_claude": "claude-sonnet-5",
    }
    assert sj._active_scoring(cfg)[0] == "gemini"


# --------------------------------------------------------------------------
# make_pool() branching
# --------------------------------------------------------------------------

class _FakeClaudePool:
    def __init__(self, *, timeout_s, max_procs):
        self.timeout_s = timeout_s
        self.max_procs = max_procs


def _install_fake_claude_cli(monkeypatch, *, cli_present: bool):
    mod = ModuleType("claude_cli")
    mod.find_claude = lambda: ("/usr/bin/claude" if cli_present else None)
    mod.ClaudePool = _FakeClaudePool
    monkeypatch.setitem(sys.modules, "claude_cli", mod)
    return mod


def test_make_pool_claude_branch_returns_fake_pool(monkeypatch, tmp_path):
    _install_fake_claude_cli(monkeypatch, cli_present=True)
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    monkeypatch.setattr(sj, "STAGE1_CONCURRENCY", 6)
    monkeypatch.setattr(sj, "STAGE2_CONCURRENCY", 4)
    monkeypatch.delenv("SCORE_CLAUDE_TIMEOUT_S", raising=False)
    pool = sj.make_pool()
    assert isinstance(pool, _FakeClaudePool)
    assert pool.timeout_s == 240  # default
    assert pool.max_procs == 6    # max(6, 4)


def test_make_pool_claude_branch_honors_timeout_env(monkeypatch, tmp_path):
    _install_fake_claude_cli(monkeypatch, cli_present=True)
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    monkeypatch.setattr(sj, "STAGE1_CONCURRENCY", 2)
    monkeypatch.setattr(sj, "STAGE2_CONCURRENCY", 3)
    monkeypatch.setenv("SCORE_CLAUDE_TIMEOUT_S", "99")
    pool = sj.make_pool()
    assert pool.timeout_s == 99
    assert pool.max_procs == 3


def test_make_pool_cli_missing_falls_back_to_gemini(monkeypatch, tmp_path, capsys):
    _install_fake_claude_cli(monkeypatch, cli_present=False)
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    fallback_pool = object()

    class _FakeKeyPool:
        @staticmethod
        def from_env(state_path):
            return fallback_pool

    monkeypatch.setattr(sj, "KeyPool", _FakeKeyPool)
    result = sj.make_pool()
    assert result is fallback_pool
    out = capsys.readouterr().out
    assert "not on PATH" in out
    assert "falling back to Gemini" in out


def test_make_pool_import_error_falls_back_never_exits(monkeypatch, tmp_path, capsys):
    # Ensure no real/fake claude_cli module is importable.
    monkeypatch.delitem(sys.modules, "claude_cli", raising=False)
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    import builtins
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "claude_cli":
            raise ImportError("no module named claude_cli")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    fallback_pool = object()

    class _FakeKeyPool:
        @staticmethod
        def from_env(state_path):
            return fallback_pool

    monkeypatch.setattr(sj, "KeyPool", _FakeKeyPool)
    result = sj.make_pool()
    assert result is fallback_pool
    out = capsys.readouterr().out
    assert "claude_cli.py is missing" in out
    assert "falling back to Gemini" in out


def test_make_pool_gemini_provider_unaffected(monkeypatch, tmp_path):
    """Sanity: provider "gemini" never even looks at claude_cli."""
    monkeypatch.delitem(sys.modules, "claude_cli", raising=False)
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    fallback_pool = object()

    class _FakeKeyPool:
        @staticmethod
        def from_env(state_path):
            return fallback_pool

    monkeypatch.setattr(sj, "KeyPool", _FakeKeyPool)
    assert sj.make_pool() is fallback_pool


# --------------------------------------------------------------------------
# score_stage1 against a fake claude-shaped pool
# --------------------------------------------------------------------------

def _claude_resp(text, in_tok=3, out_tok=5):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=in_tok, candidates_token_count=out_tok),
    )


class _ClaudeShapedPool:
    """Mirrors claude_cli.ClaudePool.generate's signature/return shape."""

    def __init__(self):
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(
            model=model, contents=contents,
            system_instruction=getattr(config, "system_instruction", None),
        ))
        return _claude_resp(json.dumps({"score": 4, "reason": "solid fit"}))


class _RaisingPool:
    async def generate(self, **kwargs):
        raise RuntimeError("cli boom")


def test_score_stage1_claude_pool_success(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    pool = _ClaudeShapedPool()
    before = dict(sj.TOKEN_USAGE)
    out = asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "RESUME TEXT", "J1", "JOB TEXT"))
    assert out == {"job_posting_id": "J1", "score": 4, "reason": "solid fit"}
    # _track_usage bumped the module-level counters.
    assert sj.TOKEN_USAGE["calls"] == before["calls"] + 1
    assert sj.TOKEN_USAGE["prompt"] == before["prompt"] + 3
    assert sj.TOKEN_USAGE["output"] == before["output"] + 5


def test_score_stage1_claude_pool_raises_yields_error_row(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    out = asyncio.run(
        sj.score_stage1(_RaisingPool(), asyncio.Semaphore(1), "resume", "J1", "job")
    )
    assert out["score"] is None
    assert out["reason"].startswith("ERROR:")


# --------------------------------------------------------------------------
# Prompt-split pins
# --------------------------------------------------------------------------

# The two literals below are the ORIGINAL pre-split STAGE1_TEMPLATE /
# STAGE2_TEMPLATE, copied VERBATIM from `git show 2304ee2:score_jobs.py`
# (the commit before the resume/job split landed). They are the frozen
# ground truth for the "gemini bytes unchanged" invariant: do NOT "fix"
# them to track score_jobs.py -- if an assertion against them fails, the
# split drifted and the SOURCE must be fixed, not these strings. Asserting
# against sj.STAGE1_TEMPLATE alone would be a tautology (the source now
# defines it as RESUME + JOB), which is exactly why these exist.
_FROZEN_STAGE1_TEMPLATE = """\
Rate how well this job matches the resume below, on a 1-5 scale.

CANDIDATE CONTEXT (read this before scoring):
TODAY'S DATE IS {today}. Judge every date in the resume and the job posting relative to that date, NOT relative to your training data. In particular, the candidate's May 2026 graduation is already in the PAST: the degree is COMPLETED and they are available to start immediately. Never treat the candidate as a current student or the degree as pending/"expected," and never lower the score because the graduation date is recent or looks like a future date to you.

This candidate is a new graduate (B.S. Computer Science, AI/ML concentration, Data Science minor, graduated May 2026 — available to start immediately) with one strong data-science internship plus substantial, advanced personal and academic projects. They are actively targeting ENTRY-LEVEL and EARLY-CAREER roles. Score with that in mind:

GEOGRAPHY / LOCATION / WORK AUTHORIZATION — IGNORE COMPLETELY: Do not factor in geography, location, onsite / hybrid / remote requirements, relocation, time zone, or work authorization at all. This job has already been vetted against the candidate's geographic preferences — regardless of where they currently live they are 100% willing to relocate, and they are authorized to work in the U.S. without sponsorship. Never raise or lower the score for location, onsite/hybrid/remote requirements, relocation, time zone, or work authorization / visa sponsorship; those have already been consented to by the candidate.

The candidate has essentially no full-time post-graduation experience yet (one internship plus strong projects) and is targeting roles a 0-experience applicant can clear. Apply this required-experience bar strictly:
  * 0 years required, OR a range with a floor of 0 ("0-2 years"), OR labeled entry-level / junior / new-grad / associate / university-grad / level "I", OR no stated experience requirement -> judge purely on SKILLS, STACK, and DOMAIN fit; a good skills match here is a 4 or 5.
  * Requires 1 or more years ("1+ years", "1-2 years", "2 years", "3+ years", etc.) -> the candidate does NOT clear the bar; this is a real gap. Cap the score at 3, and lower it toward 1-2 as the requirement or seniority rises (5+ years, OR senior/staff/principal/lead/manager/director titles -> 1-2).
  For a RANGE, use the LOWER bound: "0-2 years" clears the bar, "1-2 years" does not.
- Also score 1-2 for a hard advanced-degree requirement the candidate lacks ("Master's/PhD required"), or a genuine domain/stack mismatch where the candidate's skills do not map: low-level C/C++ kernel/embedded/firmware, hardware/electrical, or roles with NO data, analysis, or engineering component (e.g. pure quota-carrying sales, recruiting, manual non-technical QA, copywriting). Do NOT use this clause for data / analytics / BI / analyst roles — those are in-domain (see ADJACENT ANALYTICAL ROLES below).

ADJACENT ANALYTICAL ROLES ARE IN-DOMAIN (read carefully — this is a common mistake):
Treat data-analytical roles as a DOMAIN MATCH even when the title is business-flavored: Data Analyst, Business Analyst, Business Intelligence / BI Analyst, Reporting Analyst, Analytics Analyst, Product Analyst, Operations Analyst, Marketing / Research Analyst, and similar. These map directly to the candidate's SQL + Python + statistics + data-visualization / dashboarding skills (Tableau, Power BI, Looker Studio), their data-science internship, and their stakeholder / customer-facing experience. Judge such roles ONLY on whether the candidate can perform the listed RESPONSIBILITIES (querying and analyzing data, building reports/dashboards, drawing insights, communicating findings to stakeholders). Do NOT lower the score because the candidate lacks a business / finance / economics degree, because their prior experience or projects are "technical" rather than "business," or for any "career trajectory" / "career path" reason. A degree-field or job-title-history mismatch is NOT a disqualifier when the responsibilities are analytical — score these on skills like any other in-domain role (a good skills match with a 0-year floor is a 4 or 5).

Scale:
5 = Strong match - skills/domain align well AND no real experience bar (0 years / entry-level)
4 = Good match - skills align and the role has a 0-year floor / is entry-level; clearly worth applying
3 = Borderline - a real gap (requires >= 1 year, or only partial skills/domain alignment)
2 = Weak match - significant domain/stack mismatch, or 3+ years / senior seniority required
1 = No match - wrong field, or hard requirements the candidate cannot meet

Be honest and specific. Do not inflate roles that require professional experience (>= 1 year) or are off-domain. But do NOT lower the score of an otherwise-good entry-level skills fit (0-year floor) just because the candidate only graduated in May 2026 — they are a graduate, available immediately.

Resume:
---
{resume}
---

Job description:
---
{job}
---
"""

_FROZEN_STAGE2_TEMPLATE = """\
This job passed Stage 1 as a strong/good match for the candidate. Give an in-depth fit analysis: deep score 1-10, key strengths, gaps, and a recommendation.

TODAY'S DATE IS {today}. Judge every date in the resume and the job posting relative to that date, NOT relative to your training data: the candidate's May 2026 graduation is in the past and the degree is COMPLETED. Never list graduation timing, "degree in progress," or "has not graduated yet" as a gap.

Be specific. Tie strengths and gaps to concrete resume bullets and job requirements. Recommendation: "apply" (clear fit, prioritize), "consider" (mixed, depends on candidate's other options), "skip" (gaps too large despite the Stage 1 score).

When listing GAPS, name only concrete, stated requirements the candidate cannot meet: specific tools / technologies they lack, a hard credential (e.g. a required security clearance or an explicitly required advanced degree), or required years of experience. For analytical roles (Data Analyst, Business Analyst, BI / Reporting / Analytics Analyst, Product / Operations Analyst, Data Scientist), do NOT list "career trajectory," "career path," "lacks a business background/degree," "experience is technical rather than business," or similar title/degree-history mismatches as gaps — the candidate's SQL, Python, statistics, dashboarding (Tableau / Power BI / Looker), internship, and stakeholder / customer-facing experience transfer directly. Treat a title or degree-field difference as a non-issue when the candidate can do the listed work. NEVER list location, onsite / hybrid / remote, relocation, time zone, or work authorization / visa sponsorship as a gap — the candidate is fully willing to relocate and is authorized to work in the U.S. without sponsorship, so these are not gaps regardless of what the job states.

Resume:
---
{resume}
---

Job description:
---
{job}
---
"""


def test_gemini_stage1_contents_byte_identical_to_combined_template(monkeypatch):
    """Gemini lane: contents must equal the OLD (pre-split) template render --
    reconstructed here as STAGE1_TEMPLATE_RESUME + STAGE1_TEMPLATE_JOB, which
    must equal the FROZEN pre-split original embedded above -- an
    independent expected value, so a drifted split point can't self-certify."""
    assert sj.STAGE1_TEMPLATE_RESUME + sj.STAGE1_TEMPLATE_JOB == _FROZEN_STAGE1_TEMPLATE
    assert sj.STAGE1_TEMPLATE == _FROZEN_STAGE1_TEMPLATE
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    pool = _ClaudeShapedPool()  # shape-compatible; provider decides rendering
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "MY RESUME", "J1", "MY JOB"))
    call = pool.calls[0]
    expected = _FROZEN_STAGE1_TEMPLATE.format(resume="MY RESUME", job="MY JOB", today=sj.today_str())
    assert call.contents == expected
    assert call.system_instruction == sj.STAGE1_SYSTEM


def test_gemini_stage2_contents_byte_identical_to_combined_template(monkeypatch):
    assert sj.STAGE2_TEMPLATE_RESUME + sj.STAGE2_TEMPLATE_JOB == _FROZEN_STAGE2_TEMPLATE
    assert sj.STAGE2_TEMPLATE == _FROZEN_STAGE2_TEMPLATE
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    pool = _ClaudeShapedPool()

    async def _generate2(*, model, contents, config):
        pool.calls.append(SimpleNamespace(
            model=model, contents=contents,
            system_instruction=getattr(config, "system_instruction", None)))
        return _claude_resp(json.dumps(
            {"deep_score": 8, "strengths": ["s"], "gaps": ["g"], "recommendation": "apply"}))

    pool.generate = _generate2
    asyncio.run(sj.score_stage2(pool, asyncio.Semaphore(1), "MY RESUME", "J1", "MY JOB"))
    call = pool.calls[0]
    expected = _FROZEN_STAGE2_TEMPLATE.format(resume="MY RESUME", job="MY JOB", today=sj.today_str())
    assert call.contents == expected
    assert call.system_instruction == sj.STAGE2_SYSTEM


def test_claude_stage1_system_prompt_carries_resume_and_is_job_independent(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    pool = _ClaudeShapedPool()
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "UNIQUE RESUME TEXT", "J1", "job A text"))
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "UNIQUE RESUME TEXT", "J2", "job B text"))
    sys1, sys2 = pool.calls[0].system_instruction, pool.calls[1].system_instruction
    assert "UNIQUE RESUME TEXT" in sys1
    assert sys1 == sys2  # identical across two different jobs -> cache-friendly
    # contents carries only the job block -- no resume text, no other job's text.
    assert "UNIQUE RESUME TEXT" not in pool.calls[0].contents
    assert "job A text" in pool.calls[0].contents
    assert "job B text" not in pool.calls[0].contents
    assert "job B text" in pool.calls[1].contents


def test_claude_stage1_system_prompt_starts_with_stage_system(monkeypatch):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    pool = _ClaudeShapedPool()
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "resume", "J1", "job"))
    assert pool.calls[0].system_instruction.startswith(sj.STAGE1_SYSTEM)
