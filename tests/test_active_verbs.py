"""Unique leading verbs from the curated, categorized active_words.md (cycle 22).

SP1 — verb source + categorized palette + the no-reuse rule in the rephrase prompt.
SP2 — the deterministic zero-reuse guarantee (leading_verb / reverb / dedupe_leading_verbs).

No real LLM ever runs: compose.call / compose.reverb are monkeypatched. assets.active_verbs
is lru-cached, so an autouse fixture clears it around every test (the fallback test patches
the path and would otherwise poison the cache for its neighbours).
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_active_verbs_cache():
    assets.active_verbs.cache_clear()
    yield
    assets.active_verbs.cache_clear()


# --- SP1: assets.active_verbs() parse + fallback --------------------------------

EXPECTED_CATEGORIES = [
    "Communication Skills", "Creative Skills", "Data / Financial Skills",
    "Helping Skills", "Management / Leadership Skills", "Organizational Skills",
    "Research Skills", "Teaching Skills", "Technical Skills",
]


def test_active_verbs_parses_the_nine_categories_in_file_order():
    verbs = assets.active_verbs()
    assert list(verbs.keys()) == EXPECTED_CATEGORIES


def test_active_verbs_strips_separators_and_keeps_real_verbs():
    verbs = assets.active_verbs()
    comm = verbs["Communication Skills"]
    assert comm[0] == "Addressed" and comm[-1] == "Wrote"      # first/last preserved, stripped
    tech = verbs["Technical Skills"]
    assert "Engineered" in tech and "Debugged" in tech
    # No markdown debris leaked into any verb token.
    for items in verbs.values():
        assert items, "a category parsed empty"
        for v in items:
            assert v and "·" not in v and "#" not in v and v == v.strip()


def test_active_verbs_preserves_cross_category_overlap():
    # "Developed" is intentionally listed under many categories; the parse must NOT
    # globally de-dupe (each line's palette is shown under its own heading).
    verbs = assets.active_verbs()
    appears_in = [cat for cat, items in verbs.items() if "Developed" in items]
    assert len(appears_in) >= 3


def test_active_verbs_falls_back_to_builtin_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ACTIVE_WORDS_MD", tmp_path / "nope.md")
    assets.active_verbs.cache_clear()
    verbs = assets.active_verbs()
    assert verbs, "fallback must be non-empty so the engine still has a palette"
    flat = [v for items in verbs.values() for v in items]
    assert "Built" in flat and "Engineered" in flat            # the built-in _CORE_VERBS set


# --- SP1: _render_verb_palette + the rephrase prompt rule ------------------------

def test_render_verb_palette_groups_verbs_under_their_headings():
    palette = compose._render_verb_palette(
        {"Technical Skills": ["Built", "Engineered"], "Research Skills": ["Analyzed"]})
    assert "Technical Skills" in palette and "Research Skills" in palette
    assert "Built" in palette and "Engineered" in palette and "Analyzed" in palette
    # The heading precedes its own verbs (grouped, not flattened).
    assert palette.index("Technical Skills") < palette.index("Built")


def test_rephrase_prompt_carries_categorized_palette_and_no_reuse_rule(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose, "_block_of", lambda a: "Globex")
    monkeypatch.setattr(compose.assets, "example_text", lambda: "exemplar voice")
    monkeypatch.setattr(compose.assets, "active_verbs",
                        lambda: {"Technical Skills": ["Built", "Engineered", "Automated"]})
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["system"] = system
        captured["user"] = user
        return {"bullets": [{"gkey": "a1", "text": "Built A."}]}

    monkeypatch.setattr(compose, "call", fake_call)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    compose.rephrase("jd", "Eng", sel)
    assert "Technical Skills" in captured["user"]               # categorized palette injected
    assert "Automated" in captured["user"]
    blob = (captured["system"] + captured["user"]).lower()
    assert "distinct" in blob                                   # the uniqueness instruction
    assert "reuse" in blob or "never repeat" in blob
