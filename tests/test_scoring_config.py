"""Tests for score_jobs.py's externalized scoring config (scoring_config.json).

Precedence is env > config-file > built-in default. The VM runs score_jobs.py
standalone with NO json config, so the loader MUST yield today's constants. A
local user can drop a scoring_config.json next to score_jobs.py to retune the
models/concurrency/thresholds; an env var still wins over the file (the VM's
run_scraper.sh exports already override via os.environ today).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import score_jobs  # noqa: E402


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "scoring_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _clear_env(monkeypatch):
    for k in (
        "SCORE_STAGE1_MODEL", "SCORE_STAGE2_MODEL",
        "SCORE_STAGE1_CONCURRENCY", "SCORE_STAGE2_CONCURRENCY",
        "SCORE_STAGE2_THRESHOLD", "SCORE_MAX_PER_RUN", "SCORE_RESCORE_CAP",
        "SCORE_MIN_FILTER_YEARS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_absent_file_uses_builtin_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage1_model"] == "gemini-3.1-flash-lite"
    assert cfg["stage2_model"] == "gemini-3.5-flash"
    assert cfg["stage1_concurrency"] == 6
    assert cfg["stage2_concurrency"] == 4
    assert cfg["stage2_threshold"] == 4
    assert cfg["max_scored_per_run"] == 800
    assert cfg["rescore_cap"] == 200
    assert cfg["min_filter_years"] == 1
    # the module constants the existing tests rely on keep their defaults
    assert score_jobs.MIN_FILTER_YEARS == 1
    assert score_jobs.STAGE2_THRESHOLD == 4


def test_config_file_overrides_threshold_and_years(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"stage2_threshold": 3, "min_filter_years": 2,
                             "max_scored_per_run": 50})
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 3
    assert cfg["min_filter_years"] == 2
    assert cfg["max_scored_per_run"] == 50
    # untouched keys still fall back to defaults
    assert cfg["rescore_cap"] == 200
    assert cfg["stage1_model"] == "gemini-3.1-flash-lite"


def test_env_overrides_config_file(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"stage2_threshold": 3, "min_filter_years": 2})
    monkeypatch.setenv("SCORE_STAGE2_THRESHOLD", "5")
    monkeypatch.setenv("SCORE_MIN_FILTER_YEARS", "0")
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 5   # env beats the file's 3
    assert cfg["min_filter_years"] == 0   # env beats the file's 2
