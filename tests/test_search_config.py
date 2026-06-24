"""Tests for scraper.py's externalized search config (search_config.json).

The VM runs scraper.py standalone with NO json config present, so the loader
MUST fall back to the built-in module constants byte-for-byte. A local user (or
the dashboard's Settings tab) can drop a search_config.json next to scraper.py
to override keywords, the per-input limit, etc. CLI flags still win over both.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "search_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_absent_file_uses_builtin_defaults(monkeypatch, tmp_path):
    # No search_config.json -> effective values equal the module constants.
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    cfg = scraper.load_search_config()
    assert cfg["keywords"] == scraper.KEYWORDS
    assert cfg["remote_types"] == scraper.REMOTE_TYPES
    assert cfg["limit_per_input"] == scraper.LIMIT_PER_INPUT
    assert cfg["location"] == "United States"
    assert cfg["country"] == "US"
    assert cfg["time_range"] == "Past 24 hours"
    assert cfg["job_type"] == "Full-time"
    assert cfg["experience_level"] == "Entry level"


def test_file_overrides_keywords_and_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"keywords": ['"Foo"', '"Bar"'], "limit_per_input": 25})
    cfg = scraper.load_search_config()
    assert cfg["keywords"] == ['"Foo"', '"Bar"']
    assert cfg["limit_per_input"] == 25
    # untouched keys still fall back to the built-ins
    assert cfg["remote_types"] == scraper.REMOTE_TYPES


def test_build_inputs_uses_config_keywords(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"keywords": ['"One"', '"Two"', '"Three"']})
    # config keywords drive the fan-out
    inputs = scraper.build_inputs([])
    kws = {i["keyword"] for i in inputs}
    assert kws == {'"One"', '"Two"', '"Three"'}
    assert len(inputs) == 3 * len(scraper.REMOTE_TYPES)
    # max_keywords still caps the config list
    capped = scraper.build_inputs([], max_keywords=1)
    assert len({i["keyword"] for i in capped}) == 1
