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


# --- SP2: leading_verb + reverb + dedupe_leading_verbs --------------------------

SMALL_PALETTE = {"Technical Skills": ["Built", "Designed", "Engineered", "Automated", "Refactored"]}


def test_leading_verb_normalizes_first_token():
    assert compose.leading_verb("Built A.") == "built"
    assert compose.leading_verb("Engineered, a pipeline") == "engineered"   # trailing comma
    assert compose.leading_verb("   Led   the team") == "led"               # leading space
    assert compose.leading_verb("Co-developed a tool") == "co-developed"    # inner hyphen kept
    assert compose.leading_verb("") == ""
    assert compose.leading_verb("   ") == ""


def test_reverb_prompts_with_used_verbs_and_returns_text(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["system"] = system
        captured["user"] = user
        captured["tier"] = tier
        return {"text": "Designed a low-latency pipeline."}

    monkeypatch.setattr(compose, "call", fake_call)
    out = compose.reverb("jd text", ["a1"], "Built a low-latency pipeline.", {"built"})
    assert out == "Designed a low-latency pipeline."
    assert captured["tier"] == config.TIER_FLASH_LITE
    assert "built" in captured["user"].lower()                  # the taken verb is forbidden
    assert "Designed" in captured["user"]                       # palette offered


def _gm(bullets):
    return {gk: gk.split("+") for gk in bullets}


def test_dedupe_keeps_distinct_openers_without_calling_reverb(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    monkeypatch.setattr(compose, "reverb",
                        lambda *a, **k: pytest.fail("reverb must not run when all distinct"))
    bullets = {"a1": "Built A.", "b1": "Designed B."}
    out = compose.dedupe_leading_verbs(dict(bullets), _gm(bullets), "jd")
    assert out == bullets                                       # unchanged


def test_dedupe_reroll_resolves_a_collision(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    calls = {"n": 0}

    def fake_reverb(jd, ids, text, used):
        calls["n"] += 1
        return "Engineered the second thing."                  # an unused opener

    monkeypatch.setattr(compose, "reverb", fake_reverb)
    bullets = {"a1": "Built first.", "b1": "Built second."}    # collision on "built"
    out = compose.dedupe_leading_verbs(dict(bullets), _gm(bullets), "jd")
    assert out["a1"] == "Built first."                          # first occurrence keeps its verb
    assert out["b1"] == "Engineered the second thing."
    assert calls["n"] == 1
    verbs = [compose.leading_verb(t) for t in out.values()]
    assert len(set(verbs)) == len(verbs)                        # all distinct


def test_dedupe_backstop_when_model_keeps_colliding(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    # A stubborn model: the re-roll ALSO starts with "Built" -> deterministic swap must win.
    monkeypatch.setattr(compose, "reverb", lambda *a, **k: "Built it again, stubbornly.")
    bullets = {"a1": "Built one.", "b1": "Built two."}
    out = compose.dedupe_leading_verbs(dict(bullets), _gm(bullets), "jd")
    verbs = [compose.leading_verb(t) for t in out.values()]
    assert len(set(verbs)) == 2                                 # guaranteed distinct
    assert out["a1"] == "Built one."                            # first kept
    # b1 got an in-category unused verb spliced in, body preserved.
    assert compose.leading_verb(out["b1"]) in {"designed", "engineered", "automated", "refactored"}
    assert out["b1"].endswith("two.")


def test_dedupe_avoids_reserved_verbatim_verbs(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    monkeypatch.setattr(compose, "reverb", lambda *a, **k: "")   # force the backstop
    bullets = {"a1": "Built a service."}
    out = compose.dedupe_leading_verbs(dict(bullets), _gm(bullets), "jd",
                                       reserved=frozenset({"built"}))
    assert compose.leading_verb(out["a1"]) != "built"           # reserved opener avoided
    assert out["a1"].endswith("a service.")


def test_dedupe_skips_verbatim_gkeys(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    monkeypatch.setattr(compose, "reverb", lambda *a, **k: "Designed tailored.")
    vk = compose._VERBATIM_PREFIX + "/Globex/0"
    bullets = {"a1": "Built tailored.", vk: "Built verbatim, untouched."}
    out = compose.dedupe_leading_verbs(dict(bullets), {"a1": ["a1"]}, "jd")
    assert out[vk] == "Built verbatim, untouched."             # verbatim never modified


def test_dedupe_guarantees_all_distinct_even_if_model_never_helps(monkeypatch):
    monkeypatch.setattr(compose.assets, "active_verbs", lambda: SMALL_PALETTE)
    monkeypatch.setattr(compose, "reverb", lambda *a, **k: "Built yet again.")
    bullets = {f"g{i}": "Built thing %d." % i for i in range(4)}   # 4 collisions, 5-verb palette
    out = compose.dedupe_leading_verbs(dict(bullets), _gm(bullets), "jd")
    verbs = [compose.leading_verb(t) for t in out.values()]
    assert len(set(verbs)) == len(verbs)                        # zero reuse, guaranteed
