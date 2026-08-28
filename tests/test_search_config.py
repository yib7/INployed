"""Tests for scraper.py's externalized search config (search_config.json).

The VM runs scraper.py standalone with NO json config present, so the loader
MUST fall back to the built-in module constants byte-for-byte. A local user (or
the dashboard's Settings tab) can drop a search_config.json next to scraper.py
to override keywords, the per-input limit, etc. CLI flags still win over both.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

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


# P2-5: limit_per_input is interpolated into the trigger URL of a
# pay-per-collection API. load_search_config used to return whatever the JSON
# held, uncoerced, so a hand-edited or corrupted config could rewrite the
# request that gets billed.
def test_limit_per_input_is_coerced_to_a_positive_int(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    for bad in ("100&limit=5000", None, "", [], {}, 0, -3):
        _write_config(tmp_path, {"limit_per_input": bad})
        cfg = scraper.load_search_config()
        assert cfg["limit_per_input"] == scraper.LIMIT_PER_INPUT, bad
    # a numeric string is still honoured
    _write_config(tmp_path, {"limit_per_input": "40"})
    assert scraper.load_search_config()["limit_per_input"] == 40


def test_positive_int_helper():
    assert scraper._positive_int(25, 7) == 25
    assert scraper._positive_int("25", 7) == 25
    assert scraper._positive_int(2.9, 7) == 2        # int() truncates, still >= 1
    assert scraper._positive_int(0, 7) == 7
    assert scraper._positive_int("100&limit=5000", 7) == 7
    assert scraper._positive_int(None, 7) == 7


def test_trigger_url_cannot_carry_an_injected_query_parameter(monkeypatch):
    """Even called directly with a poisoned limit, trigger() must not smuggle a
    second parameter into the billed URL."""
    import asyncio

    seen = {}

    class _Resp:
        status = 200

        async def json(self):
            return {"snapshot_id": "s1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, url, json=None):
            seen["url"] = url
            return _Resp()

    asyncio.run(scraper.trigger(_Session(), {}, "100&limit=5000"))
    assert "&limit=5000" not in seen["url"]
    assert f"limit_per_input={scraper.LIMIT_PER_INPUT}" in seen["url"]


# --- exclude_window_days ------------------------------------------------------
# The dashboard's Settings tab writes exclude_window_days into search_config.json,
# but scraper.exclude_window_days() only ever read the EXCLUDE_WINDOW_DAYS env var,
# so a user-set window was silently ignored and every run fell back to the 90-day
# default. That kept the whole master in jobs_to_not_include and grew the trigger
# POST until Bright Data rejected it (2026-08-26).

def test_exclude_window_days_honours_search_config(monkeypatch, tmp_path):
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"exclude_window_days": 14})
    assert scraper.exclude_window_days() == 14


def test_exclude_window_days_env_var_still_wins(monkeypatch, tmp_path):
    """env > file > default, matching every other override in this module."""
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "30")
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"exclude_window_days": 14})
    assert scraper.exclude_window_days() == 30


def test_exclude_window_days_falls_back_to_the_default(monkeypatch, tmp_path):
    """The VM runs with no config file and no env var — behaviour must not change."""
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    assert scraper.exclude_window_days() == scraper.DEFAULT_EXCLUDE_WINDOW_DAYS


def test_junk_exclude_window_in_config_falls_back(monkeypatch, tmp_path):
    """A hand-edited file must not be able to zero the window: a 0/negative/garbage
    value would empty the exclude set and re-bill every posting."""
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    for bad in ("banana", 0, -5, None):
        _write_config(tmp_path, {"exclude_window_days": bad})
        assert scraper.exclude_window_days() == scraper.DEFAULT_EXCLUDE_WINDOW_DAYS


def test_a_junk_env_var_falls_through_to_the_file_not_past_it(monkeypatch, tmp_path):
    """Resolution is env > file > default, and a BROKEN env leg must not skip the
    file leg. It did: a typo in EXCLUDE_WINDOW_DAYS silently overrode the window
    the user had set in the dashboard, and put the 90-day default back."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    (tmp_path / "search_config.json").write_text(
        json.dumps({"exclude_window_days": 14}), encoding="utf-8")
    for junk in ("banana", "0", "-5", "  "):
        monkeypatch.setenv("EXCLUDE_WINDOW_DAYS", junk)
        assert scraper.exclude_window_days() == 14, f"{junk!r} skipped the file"


def test_with_no_file_a_junk_env_var_still_lands_on_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("EXCLUDE_WINDOW_DAYS", "banana")
    assert scraper.exclude_window_days() == scraper.DEFAULT_EXCLUDE_WINDOW_DAYS
