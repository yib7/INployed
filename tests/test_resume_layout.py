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


from resume_tailor import compose  # noqa: E402


def _fake_sel():
    # Two experience blocks, two leadership orgs, two projects, over-filled.
    return {
        "experience": [
            {"name": "Globex", "groups": [["a1"], ["a2"], ["a3"], ["a4"]]},
            {"name": "Initech", "groups": [["b1"], ["b2"], ["b3"]]},
        ],
        "leadership": [
            {"name": "NRHH", "groups": [["c1"], ["c2"]]},
            {"name": "RHA", "groups": [["d1"], ["d2"]]},
        ],
        "projects": [
            {"name": "P1", "groups": [["e1"], ["e2"], ["e3"]]},
            {"name": "P2", "groups": [["f1"]]},
            {"name": "P3", "groups": [["g1"]]},
            {"name": "P4", "groups": [["h1"]]},
        ],
    }


def test_cap_projects_limits_count_and_bullets(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = _fake_sel()
    compose._cap_projects(sel)
    assert len(sel["projects"]) == config.PROJECTS_MAX           # 4 -> 3
    assert len(sel["projects"][0]["groups"]) == config.PROJECT_BULLETS_MAX  # 3 -> 2


def test_bullet_line_targets_maps_each_bullet(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_layout": {
        "Globex": {"line_targets": [2, 1]},
        "NRHH": {"line_targets": [2]},
    }})
    sel = {
        "experience": [{"name": "Globex", "groups": [["a1"], ["a2"]]}],
        "leadership": [{"name": "NRHH", "groups": [["c1"]]}],
        "projects": [{"name": "P1", "groups": [["e1"]]}],
    }
    tgt = compose.bullet_line_targets(sel)
    assert tgt[compose._gkey(["a1"])] == 2
    assert tgt[compose._gkey(["a2"])] == 1
    assert tgt[compose._gkey(["c1"])] == 2
    assert tgt[compose._gkey(["e1"])] == config.PROJECT_BULLET_LINES


def test_enforce_fixed_counts_fallback_to_default_line_targets(monkeypatch):
    # "Globex" is NOT in the resume_layout config below, so block_targets("Globex")
    # falls back to DEFAULT_LINE_TARGETS (length 3).  With 4 atoms available it
    # must be resized DOWN to exactly 3 bullet groups.
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_layout": {
        "Initech": {"line_targets": [2, 1]},  # some OTHER block — Globex absent
    }})
    sel = {
        "experience": [
            {"name": "Globex", "groups": [["a1"], ["a2"], ["a3"], ["a4"]]},
        ],
        "leadership": [],
        "projects": [],
    }
    compose._enforce_fixed_counts(sel)
    octus = sel["experience"][0]
    assert len(octus["groups"]) == len(config.DEFAULT_LINE_TARGETS)  # must be 3


import inspect


def test_length_hint_is_plain_lines():
    assert compose._length_hint(1) == "about 1 line (<= 100 characters)"
    assert compose._length_hint(2) == "about 2 lines (<= 200 characters)"


def test_rephrase_dropped_budgets_param():
    assert "budgets" not in inspect.signature(compose.rephrase).parameters


from resume_tailor import run as rt_run  # noqa: E402


def test_trim_to_caps_trims_over_length(monkeypatch):
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_layout": {"RHA": {"line_targets": [1]}}})
    sel = {"experience": [], "leadership": [{"name": "RHA", "groups": [["d1"]]}],
           "projects": []}
    gk = compose._gkey(["d1"])
    long_text = "Led " + "x" * 200  # ~204 chars, target 1 line -> cap 100
    bullets = {gk: long_text}
    rt_run._trim_to_caps(sel, bullets)
    assert len(bullets[gk]) <= config.MAX_LINE_CHARS
    assert bullets[gk].startswith("Led")  # front-loaded content preserved


def test_trim_to_caps_leaves_short_bullets(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "leadership": [], "projects": []}
    gk = compose._gkey(["a1"])
    short = "Built a small pipeline"
    bullets = {gk: short}
    rt_run._trim_to_caps(sel, bullets)
    assert bullets[gk] == short  # under cap (2 lines = 200) -> untouched, never padded


def test_refit_and_body_math_removed():
    assert not hasattr(compose, "refit")
    assert not hasattr(compose, "layout_budgets")
    assert not hasattr(rt_run, "_enforce_layout")
    from resume_tailor import layout
    assert not hasattr(layout, "body_line_budget")
    assert not hasattr(layout, "body_fits")
