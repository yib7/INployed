"""LLM JSON-mode responses whose ROOT is an array, not the promised object.

Gemini occasionally roots a json_out answer at an ARRAY: either the requested
object wrapped in a one-element array ([{...}]) or the bare array that belonged
under the wrapper key ({"bullets": [...]} returned as [{"gkey": ...}, ...]).
compose used to call .get() straight on that root, so ONE bad-shape response
killed the whole tailor job with "'list' object has no attribute 'get'" — even
in stages documented as advisory/never-fatal (observed: 1 of 7 concurrent jobs).

llm.as_dict is the single normalizer: both array shapes recover losslessly and
any other root coerces to {}, so every stage degrades to its no-result path.
No real LLM runs here: compose.call is monkeypatched throughout.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose, llm  # noqa: E402


# ── the normalizer itself ─────────────────────────────────────────────────────
def test_as_dict_passes_object_through():
    obj = {"bullets": [{"gkey": "a1", "text": "T"}]}
    assert llm.as_dict(obj, "bullets") is obj


def test_as_dict_unwraps_array_wrapped_object():
    inner = {"bullets": [{"gkey": "a1", "text": "T"}]}
    assert llm.as_dict([inner], "bullets") == inner


def test_as_dict_restores_dropped_wrapper():
    items = [{"gkey": "a1", "text": "T"}, {"gkey": "a2", "text": "U"}]
    assert llm.as_dict(items, "bullets") == {"bullets": items}


def test_as_dict_skips_non_dict_items():
    items = ["noise", {"gkey": "a1", "text": "T"}, 42]
    assert llm.as_dict(items, "bullets") == {"bullets": [{"gkey": "a1", "text": "T"}]}


@pytest.mark.parametrize("garbage", [None, "prose", 5, [], ["a", "b"], [[]]])
def test_as_dict_garbage_roots_become_empty(garbage):
    assert llm.as_dict(garbage, "bullets") == {}


# ── select: the load-bearing first stage ──────────────────────────────────────
def test_select_survives_array_wrapped_selection(monkeypatch):
    wrapped = [{"experience": [], "projects": [], "leadership": [],
                "skill_focus": "general", "skills": {}, "rationale": ""}]
    monkeypatch.setattr(compose, "call", lambda *a, **k: wrapped)
    out = compose.select("Analyze data in Python and SQL.", "Data Analyst", "CalPERS")
    assert isinstance(out, dict) and "experience" in out


def _fake_assets(monkeypatch):
    """Minimal one-block world so _normalize_selection runs without the real master."""
    monkeypatch.setattr(compose.assets, "atoms_by_id",
                        lambda: {"a1": {"_block": "Globex"}, "a2": {"_block": "Globex"}})
    monkeypatch.setattr(compose.assets, "blocks", lambda: {
        "experience": [{"name": "Globex", "atoms": ["a1", "a2"]}],
        "projects": [], "leadership": []})
    monkeypatch.setattr(compose, "_required_blocks", lambda: {})
    monkeypatch.setattr(compose.config, "_config_json", lambda: {})


def test_normalize_selection_survives_malformed_model_shapes(monkeypatch):
    _fake_assets(monkeypatch)
    sel = {
        "skill_focus": "general",
        "skills": ["Python", "SQL"],       # list where the prompt promised an object
        "methods": "not-a-list",
        "experience": ["junk",             # entry as a bare string
                       {"name": "Globex",
                        "groups": ["a1", ["a2"], None]}],  # flat id + junk group
        "projects": None,
        "leadership": [],
    }
    out = compose._normalize_selection(sel)
    # A list-valued skills key would crash compress_skills' preselected path later.
    assert out["skills"] == {}
    flat = [a for e in out["experience"] for g in e["groups"] for a in g]
    assert "a1" in flat and "a2" in flat   # flat string group recovered, junk dropped


def test_normalize_selection_non_dict_root(monkeypatch):
    _fake_assets(monkeypatch)
    out = compose._normalize_selection(["total", "garbage"])
    assert isinstance(out, dict) and out["skills"] == {}


# ── rephrase: bullets must recover losslessly ─────────────────────────────────
def _rephrase_scaffold(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose, "_block_of", lambda a: "Globex")
    monkeypatch.setattr(compose.assets, "example_text", lambda: "exemplar")


_SEL = {"experience": [{"name": "Globex", "groups": [["a1"], ["a2"]]}],
        "projects": [], "leadership": []}


def test_rephrase_recovers_bullets_from_bare_array(monkeypatch):
    _rephrase_scaffold(monkeypatch)
    monkeypatch.setattr(compose, "call", lambda *a, **k: [
        {"gkey": "a1", "text": "Built A."}, {"gkey": "a2", "text": "Built B."}])
    assert compose.rephrase("jd", "Eng", _SEL) == {"a1": "Built A.", "a2": "Built B."}


def test_rephrase_recovers_bullets_from_array_wrapped_object(monkeypatch):
    _rephrase_scaffold(monkeypatch)
    monkeypatch.setattr(compose, "call", lambda *a, **k: [
        {"bullets": [{"gkey": "a1", "text": "Built A."}]}])
    assert compose.rephrase("jd", "Eng", _SEL) == {"a1": "Built A."}


def test_rephrase_skips_non_dict_bullet_items(monkeypatch):
    _rephrase_scaffold(monkeypatch)
    monkeypatch.setattr(compose, "call", lambda *a, **k: {
        "bullets": ["noise", {"gkey": "a1", "text": "Built A."}, 42]})
    assert compose.rephrase("jd", "Eng", _SEL) == {"a1": "Built A."}


def test_rephrase_survives_null_bullets_value(monkeypatch):
    _rephrase_scaffold(monkeypatch)
    monkeypatch.setattr(compose, "call", lambda *a, **k: {"bullets": None})
    assert compose.rephrase("jd", "Eng", _SEL) == {}


# ── the advisory stages must honor their never-fatal contracts ────────────────
def test_block_briefs_never_fatal_on_array_root(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": "x"})
    monkeypatch.setattr(compose, "call", lambda *a, **k: ["nonsense"])
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    assert compose.block_briefs("jd", "Eng", sel) == {}


def test_block_briefs_recovers_bare_array_briefs(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": "x"})
    monkeypatch.setattr(compose, "call", lambda *a, **k: [
        {"block": "Globex", "brief": "Backend platform work."}])
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    assert compose.block_briefs("jd", "Eng", sel) == {"Globex": "Backend platform work."}


def test_enforce_style_recovers_repair_from_bare_array(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    sel = {"experience": [{"name": "X", "groups": [["a1"], ["a2"]]}],
           "projects": [], "leadership": []}
    bullets = {"a1": "Cut latency by 30%, ensuring fast responses",
               "a2": "Built the sandbox — no network"}
    monkeypatch.setattr(compose, "call", lambda *a, **k: [
        {"gkey": "a1", "text": "Cut latency by 30% so responses stay fast"}])
    changed = compose.enforce_style("jd", "Engineer", sel, bullets)
    assert bullets["a1"] == "Cut latency by 30% so responses stay fast"  # recovered
    assert "—" not in bullets["a2"]        # em-dash backstop still ran
    assert changed == 2


# ── skills: array root falls back to pool completion, never a crash ───────────
def test_compress_skills_survives_array_root(monkeypatch):
    monkeypatch.setattr(compose, "call", lambda *a, **k: ["junk"])
    lines = compose.compress_skills("jd", "Data Analyst", {})
    assert [ln["label"] for ln in lines] == [
        "Languages", "Frameworks", "Developer Tools", "Libraries"]


def test_finalize_skill_lines_coerces_non_string_values():
    out = {"Languages": ["Python", "SQL"], "Frameworks": 7,
           "Developer Tools": None, "Libraries": {"x": 1}}
    lines = compose._finalize_skill_lines(out)          # must not raise
    assert all(isinstance(ln["items"], str) for ln in lines)


def test_finalize_skill_lines_non_dict_root():
    lines = compose._finalize_skill_lines(["Python", "SQL"])  # must not raise
    assert isinstance(lines, list)
