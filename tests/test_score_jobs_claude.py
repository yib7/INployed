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

def test_gemini_stage1_contents_byte_identical_to_combined_template(monkeypatch):
    """Gemini lane: contents must equal the OLD (pre-split) template render --
    reconstructed here as STAGE1_TEMPLATE_RESUME + STAGE1_TEMPLATE_JOB, which
    is by construction == STAGE1_TEMPLATE (assert that equality too, so a
    future edit to either half can't silently drift the concatenation)."""
    assert sj.STAGE1_TEMPLATE == sj.STAGE1_TEMPLATE_RESUME + sj.STAGE1_TEMPLATE_JOB
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    pool = _ClaudeShapedPool()  # shape-compatible; provider decides rendering
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "MY RESUME", "J1", "MY JOB"))
    call = pool.calls[0]
    expected = sj.STAGE1_TEMPLATE.format(resume="MY RESUME", job="MY JOB", today=sj.today_str())
    assert call.contents == expected
    assert call.system_instruction == sj.STAGE1_SYSTEM


def test_gemini_stage2_contents_byte_identical_to_combined_template(monkeypatch):
    assert sj.STAGE2_TEMPLATE == sj.STAGE2_TEMPLATE_RESUME + sj.STAGE2_TEMPLATE_JOB
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
    expected = sj.STAGE2_TEMPLATE.format(resume="MY RESUME", job="MY JOB", today=sj.today_str())
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
