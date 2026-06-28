"""Resume layout: config precedence, count caps, deterministic trim, line-target map.
No LLM, no UI."""
import inspect
import shutil
import sys
from math import ceil
from pathlib import Path

import pytest

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
    assert config.MAX_LINE_CHARS == 130
    assert config.PROJECTS_MAX == 3 and config.PROJECT_BULLETS_MAX == 2
    assert config.PROJECTS_MAX_LIMIT == 6


def test_projects_max_default(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.projects_max() == config.PROJECTS_MAX  # 3 when nothing is set


def test_projects_max_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": 4})
    assert config.projects_max() == 4


def test_projects_max_clamped_to_limit(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": 99})
    assert config.projects_max() == config.PROJECTS_MAX_LIMIT
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": 0})
    assert config.projects_max() == 1


def test_projects_max_bad_value_falls_back(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": "lots"})
    assert config.projects_max() == config.PROJECTS_MAX


def test_projects_max_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROJECTS_MAX", "5")
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": 2})
    assert config.projects_max() == 5


def test_projects_mode_default_is_max(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MODE", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.projects_mode() == "max"


def test_projects_mode_from_config_json(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MODE", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_mode": "exact"})
    assert config.projects_mode() == "exact"


def test_projects_mode_bad_value_falls_back_to_max(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MODE", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_mode": "weird"})
    assert config.projects_mode() == "max"


def test_projects_mode_env_overrides_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_PROJECTS_MODE", "exact")
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_mode": "max"})
    assert config.projects_mode() == "exact"


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
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = _fake_sel()
    compose._cap_projects(sel)
    assert len(sel["projects"]) == config.PROJECTS_MAX           # 4 -> 3
    assert len(sel["projects"][0]["groups"]) == config.PROJECT_BULLETS_MAX  # 3 -> 2


def test_cap_projects_honors_configured_max(monkeypatch):
    # With the dashboard cap raised to 4, all four projects are kept (not trimmed to 3).
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"projects_max": 4})
    sel = _fake_sel()
    compose._cap_projects(sel)
    assert len(sel["projects"]) == 4


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


def test_length_hint_has_floor_and_ceiling():
    """A3: the hint now carries a soft floor (>=90% single-line, >=75% last line of
    a multi-line bullet) plus the hard ceiling = target_lines * MAX_LINE_CHARS."""
    per = config.MAX_LINE_CHARS
    h1 = compose._length_hint(1)
    assert str(per) in h1                       # ceiling = one line
    assert str(ceil(0.90 * per)) in h1          # single-line floor >=90%
    h2 = compose._length_hint(2)
    assert str(2 * per) in h2                    # ceiling = two lines
    assert str(ceil((1 + 0.75) * per)) in h2    # multi-line last-line floor >=75%


def test_select_uses_four_skill_pools_no_keyerror(monkeypatch):
    """A2: select() builds its prompt from the 4 skill-pool keys; the old 3-bucket
    references would KeyError before the LLM is even called."""
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["system"] = system
        captured["user"] = user
        return {"experience": [], "projects": [], "leadership": [],
                "skill_focus": "general", "skills": {}, "rationale": ""}

    monkeypatch.setattr(compose, "call", fake_call)
    compose.select("Build data pipelines in Python and SQL.", "Data Analyst", "ACME")
    for label in ("Languages:", "Frameworks:", "Developer Tools:", "Libraries:"):
        assert label in captured["user"]
    assert "Tools & Infrastructure" not in captured["user"]
    assert "Libraries & Frameworks" not in captured["user"]
    assert "exactly four lines" in captured["system"]


def test_compress_skills_returns_four_labeled_lines():
    """A2: the preselected-skills path returns all 4 fixed lines, each non-empty and
    within its best-N target count."""
    pre = {"skills": {"Languages": "Python, SQL", "Frameworks": "Flask",
                      "Developer Tools": "Git", "Libraries": "NumPy"}}
    lines = compose.compress_skills("jd", "Data Analyst", pre)
    assert [ln["label"] for ln in lines] == [
        "Languages", "Frameworks", "Developer Tools", "Libraries"]
    assert all(ln["items"].strip() for ln in lines)
    targets = compose.layout.skill_targets()
    for ln in lines:                                   # never exceed best-N per line
        assert len(ln["items"].split(", ")) <= targets[ln["label"]]


def test_complete_to_count_keeps_model_order_then_completes_from_pool():
    """SP1: the model's relevance order leads; the pool completes up to the target."""
    pool = ["Python", "SQL", "C", "Java", "R", "Go", "Rust", "Kotlin"]
    assert compose._complete_to_count("Go, Python", pool, 5) == [
        "Go", "Python", "SQL", "C", "Java"]                # model 2 first, then pool order


def test_complete_to_count_caps_at_target():
    pool = ["Python", "SQL", "C", "Java", "R", "Go", "Rust", "Kotlin", "Scala", "Perl"]
    out = compose._complete_to_count(", ".join(pool), pool, 7)
    assert out == pool[:7]                                 # 10 available -> top 7 only


def test_complete_to_count_pool_smaller_than_target_takes_all_no_padding():
    """A short pool yields a short line — NO floor padding (the killed behavior)."""
    assert compose._complete_to_count("Python, SQL", ["Python", "SQL"], 7) == ["Python", "SQL"]


def test_complete_to_count_single_char_skills_not_falsely_skipped():
    """'C'/'R' must not be dropped as substrings of 'JavaScript' during completion."""
    out = compose._complete_to_count("JavaScript", ["JavaScript", "C", "R", "Python"], 4)
    assert out == ["JavaScript", "C", "R", "Python"]


def test_complete_to_count_preserves_merged_token_and_skips_its_components():
    pool = ["Gemini", "OpenAI", "Claude", "Git", "Docker"]
    out = compose._complete_to_count("Gemini/OpenAI/Claude API", pool, 3)
    assert out == ["Gemini/OpenAI/Claude API", "Git", "Docker"]   # components skipped


def test_finalize_skill_lines_drops_bottom_to_fit_one_line(monkeypatch):
    """SP1: when the chosen items overflow one printed line, the least-relevant tail
    is dropped until they fit — never padded, never wrapped."""
    monkeypatch.setattr(compose, "_skill_pools", lambda: {
        "Languages": [], "Frameworks": [], "Developer Tools": [], "Libraries": []})
    monkeypatch.setattr(compose.layout, "skill_targets", lambda: {
        "Languages": 7, "Frameworks": 0, "Developer Tools": 0, "Libraries": 0})
    monkeypatch.setattr(compose.layout, "skill_caps", lambda: {
        "Languages": 14, "Frameworks": 0, "Developer Tools": 0, "Libraries": 0})
    out = {"Languages": "Python, SQL, JavaScript, TypeScript"}
    lines = compose._finalize_skill_lines(out)
    by = {ln["label"]: ln["items"] for ln in lines}
    assert by["Languages"] == "Python, SQL"               # ", JavaScript" would exceed 14 -> dropped
    assert "Frameworks" not in by                          # empty pool + no items -> no line


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


def test_word_trim_ends_on_clean_boundary():
    """Over-length bullets must never end mid-clause on a dangling connective
    (the 'utilizing Gemini Flash to.' / 'while maintaining.' bug)."""
    cases = [
        ("Streamlined transactions for 100+ customers per shift during peak "
         "periods while maintaining accuracy", 100),
        ("Architected a multi-model pipeline inside a hardened Docker sandbox "
         "with a 60s timeout and 16MB upload cap, utilizing Gemini Flash to "
         "moderate content", 200),
        ("Quantified the driving factors behind public support for social "
         "policies by applying OLS regression to survey data, identifying "
         "ideological alignment as the primary driver of polarization", 200),
        ("Built an AI platform to train a Gradient Boosting Classifier that "
         "achieved high accuracy across many statistical features extracted "
         "from raw packet captures via Wireshark", 200),
    ]
    dangling = {"to", "that", "of", "like", "with", "by", "while", "and",
                "using", "utilizing", "for", "as", "the", "a", "an"}
    for text, cap in cases:
        out = rt_run._word_trim(text, cap)
        assert len(out) <= cap - 1
        assert out.split()[-1].lower().strip(",;:") not in dangling, out


def test_word_trim_keeps_thousands_separator():
    """A comma inside a number (4,000) is not a clause boundary to cut on."""
    text = ("Led community service efforts, earning membership among the top "
            "1% of 4,000+ residence-hall leaders nationwide")
    out = rt_run._word_trim(text, 100)
    assert not out.endswith("4")
    assert out.split()[-1].lower() not in {"of", "top"}


def test_trim_to_caps_leaves_short_bullets(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "leadership": [], "projects": []}
    gk = compose._gkey(["a1"])
    short = "Built a small pipeline"
    bullets = {gk: short}
    rt_run._trim_to_caps(sel, bullets)
    assert bullets[gk] == short  # under cap (2 lines = 200) -> untouched, never padded


def test_verbatim_blocks_reader_sanitizes(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"verbatim_blocks": {
        "Globex": ["Did a thing", "  ", "Did another"],   # blanks dropped
        "Empty": [],                                        # empty list dropped
        "Bad": "nope",                                      # non-list dropped
    }})
    assert config.verbatim_blocks() == {"Globex": ["Did a thing", "Did another"]}


def test_inject_verbatim_replaces_groups_and_excludes_from_group_map(monkeypatch):
    monkeypatch.setattr(config, "verbatim_blocks",
                        lambda: {"Globex": ["My exact bullet one", "My exact bullet two"]})
    sel = {
        "experience": [{"name": "Globex", "groups": [["a1"], ["a2"]]},
                       {"name": "Initech", "groups": [["b1"]]}],
        "projects": [], "leadership": [],
    }
    verbatim = compose.inject_verbatim(sel)
    gks = [compose._gkey(ids) for ids in sel["experience"][0]["groups"]]
    assert all(compose.is_verbatim_gkey(gk) for gk in gks)          # Globex groups -> verbatim
    assert set(verbatim.values()) == {"My exact bullet one", "My exact bullet two"}
    assert all(compose.is_verbatim_gkey(gk) for gk in verbatim)
    gm = compose.group_map(sel)
    assert compose._gkey(["b1"]) in gm                              # Initech still tailored
    assert not any(compose.is_verbatim_gkey(gk) for gk in gm)       # verbatim excluded from LLM


def test_inject_verbatim_noop_without_config(monkeypatch):
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    assert compose.inject_verbatim(sel) == {}
    assert sel["experience"][0]["groups"] == [["a1"]]              # untouched


def test_trim_to_caps_never_trims_verbatim(monkeypatch):
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {"RHA": ["x" * 400]})
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_layout": {"RHA": {"line_targets": [1]}}})
    sel = {"experience": [], "leadership": [{"name": "RHA", "groups": [["d1"]]}],
           "projects": []}
    verbatim = compose.inject_verbatim(sel)
    gk = next(iter(verbatim))
    bullets = dict(verbatim)
    rt_run._trim_to_caps(sel, bullets)
    assert bullets[gk] == "x" * 400                                # exact text, never trimmed


def test_block_briefs_one_batched_call_per_block(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    seen = {"calls": 0}

    def fake_call(system, user, tier, **kw):
        seen["calls"] += 1
        seen["tier"] = tier
        return {"briefs": [{"block": "Globex", "brief": "Backend platform work."},
                           {"block": "Bogus", "brief": "ignored — not a block"}]}

    monkeypatch.setattr(compose, "call", fake_call)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"], ["a2"]]}],
           "projects": [], "leadership": []}
    out = compose.block_briefs("jd", "Engineer", sel)
    assert out == {"Globex": "Backend platform work."}   # unknown block names dropped
    assert seen["calls"] == 1                             # ONE batched call
    assert seen["tier"] == config.TIER_FLASH_LITE         # cheapest tier


def test_block_briefs_swallows_call_failure(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": "x"})

    def boom(*a, **k):
        raise RuntimeError("no creds")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    assert compose.block_briefs("jd", "Eng", sel) == {}   # advisory: never fatal


def test_rephrase_groups_by_block_and_threads_brief(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose, "_block_of",
                        lambda a: "Globex" if a.startswith("a") else "P1")
    monkeypatch.setattr(compose.assets, "example_text", lambda: "exemplar voice")
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["system"] = system
        captured["user"] = user
        return {"bullets": [{"gkey": "a1", "text": "Built A."},
                            {"gkey": "a2+a3", "text": "Fused A2/A3."},
                            {"gkey": "e1", "text": "Made P1."}]}

    monkeypatch.setattr(compose, "call", fake_call)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"], ["a2", "a3"]]}],
           "projects": [{"name": "P1", "groups": [["e1"]]}], "leadership": []}
    briefs = {"Globex": "Platform reliability story.", "P1": "An ML side project."}
    out = compose.rephrase("jd", "Eng", sel, briefs=briefs)
    assert out == {"a1": "Built A.", "a2+a3": "Fused A2/A3.", "e1": "Made P1."}
    assert "Platform reliability story." in captured["user"]   # briefs threaded in
    assert "An ML side project." in captured["user"]
    assert "COHESION" in captured["system"]                    # cohesion instruction present


def test_rephrase_backward_compatible_without_briefs(monkeypatch):
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(compose, "_block_of", lambda a: "Globex")
    monkeypatch.setattr(compose.assets, "example_text", lambda: "exemplar")
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: {"bullets": [{"gkey": "a1", "text": "Built A."}]})
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    assert compose.rephrase("jd", "Eng", sel) == {"a1": "Built A."}   # briefs optional


def test_refit_and_body_math_removed():
    assert not hasattr(compose, "refit")
    assert not hasattr(compose, "layout_budgets")
    assert not hasattr(rt_run, "_enforce_layout")
    from resume_tailor import layout
    assert not hasattr(layout, "body_line_budget")
    assert not hasattr(layout, "body_fits")


def test_drop_weakest_keep_projects_never_empties_a_project():
    """B2 exact mode: only multi-bullet projects are trimmed; a project's last
    bullet is never dropped, so no project vanishes and the count holds."""
    from resume_tailor import compile as rt_compile
    sel = {"projects": [
        {"name": "P1", "groups": [["e1"], ["e2"]]},
        {"name": "P2", "groups": [["f1"]]},  # single bullet — must survive
    ]}
    bullets = {"e1": "x", "e2": "y", "f1": "z"}
    assert rt_compile._drop_weakest_group(sel, bullets, keep_projects=True) == "e2"
    # nothing else can go without emptying a project -> None, count preserved
    assert rt_compile._drop_weakest_group(sel, bullets, keep_projects=True) is None
    assert set(bullets) == {"e1", "f1"}


def test_drop_weakest_max_mode_can_drop_a_whole_project():
    """Default (at-most) mode may drop a project's only bullet to fit one page."""
    from resume_tailor import compile as rt_compile
    sel = {"projects": [{"name": "P1", "groups": [["e1"]]}]}
    bullets = {"e1": "x"}
    assert rt_compile._drop_weakest_group(sel, bullets, keep_projects=False) == "e1"
    assert bullets == {}


def test_enforce_one_page_resolves_keep_projects_from_config(tmp_path, monkeypatch):
    """enforce_one_page derives keep_projects from config.projects_mode() when the
    caller doesn't pass it (exact -> keep_projects True)."""
    from resume_tailor import compile as rt_compile
    monkeypatch.setattr(config, "projects_mode", lambda: "exact")
    monkeypatch.setattr(rt_compile.render, "render", lambda *a, **k: "TEX")
    monkeypatch.setattr(rt_compile, "compile_tex",
                        lambda tex_path, work_dir: rt_compile.CompileResult(True, tex_path, ""))
    monkeypatch.setattr(rt_compile, "page_count", lambda p: 2)  # always "too long"
    seen = {}

    def fake_drop(sel, bullets, keep_projects=False):
        seen["keep"] = keep_projects
        return None  # stop the loop immediately (best-effort)

    monkeypatch.setattr(rt_compile, "_drop_weakest_group", fake_drop)
    rt_compile.enforce_one_page({"projects": []}, {"g": "x"}, [],
                                tmp_path / "r.tex", tmp_path)
    assert seen["keep"] is True


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
def test_enforce_one_page_drops_until_one_page(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    from resume_tailor import assets, compile as rt_compile
    bl = assets.blocks()
    sel = {"experience": [], "projects": [], "leadership": [],
           "skill_focus": "general", "skills": {}, "rationale": ""}
    for sec in ("experience", "projects", "leadership"):
        for b in bl[sec]:
            if b["atoms"]:
                sel[sec].append({"name": b["name"], "groups": [[a] for a in b["atoms"]]})
    sel = compose._normalize_selection(sel)
    gm = compose.group_map(sel)
    # Deliberately bloated bullets to force >1 page.
    bullets = {gk: ("Engineered a large multi-stage system " * 6).strip() + "." for gk in gm}
    skills = [{"label": "Languages", "items": "Python, SQL"},
              {"label": "Frameworks", "items": "Flask, Django"},
              {"label": "Developer Tools", "items": "Git, Docker"},
              {"label": "Libraries", "items": "Pandas, NumPy"}]
    res, final, _tex = rt_compile.enforce_one_page(
        sel, bullets, skills, tmp_path / "r.tex", tmp_path, jd="x" * 50)
    assert res.ok
    assert rt_compile.page_count(res.pdf_path) == 1
    assert not hasattr(rt_compile, "compose")  # compile must never import compose
