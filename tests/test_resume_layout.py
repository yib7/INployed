"""Resume layout: config precedence, count caps, deterministic trim, line-target map.
No LLM, no UI."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config  # noqa: E402


def test_block_targets_default_when_no_config(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.block_targets("Globex") == config.DEFAULT_LINE_TARGETS


def test_block_targets_from_config_json(monkeypatch):
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_layout": {"Initech": {"line_targets": [2]}}})
    assert config.block_targets("Initech") == [2]
    assert config.block_targets("Globex") == config.DEFAULT_LINE_TARGETS  # unlisted -> default


def test_block_targets_sanitizes_ints_and_length(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_layout": {
        "X": {"line_targets": [9, 0, "2", 1, 1, 1, 1]},  # clamp ints to 1-3, list to <=5
    }})
    assert config.block_targets("X") == [3, 1, 2, 1, 1]


def test_block_targets_bad_shape_falls_back(monkeypatch):
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_layout": {"X": {"line_targets": "nope"}}})
    assert config.block_targets("X") == config.DEFAULT_LINE_TARGETS


def test_constants_present():
    assert config.MAX_LINE_CHARS == 100
    assert config.PROJECTS_MAX == 3 and config.PROJECT_BULLETS_MAX == 2
