"""Tests for the config-driven résumé layout (PLAN stage 3).

The compose/layout/render stack must NOT hardcode any one person's employers:
which blocks are required and the fixed per-block line budgets come from the
optional `tailor:` section of master_experience.yaml, and the name/contact header
+ Education are rendered from the yaml `basics`/`education` (not the template).

These tests swap in a synthetic, generic master_experience.yaml so they prove the
behaviour for *any* user, not just the repo owner.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config, output, render  # noqa: E402

_CACHED = (
    assets.load_master, assets.tailor_config, assets.atoms_by_id,
    assets.blocks, assets.template_head,
)

_MASTER = textwrap.dedent("""
    tailor:
      fixed_blocks:
        Side Gig:
          line_targets: [2, 1]
      leadership_entry_lines: 2
    basics:
      name: Jane Q. Public
      location: City, ST
      email: jane@example.com
      phone: "555-0100"
    education:
      - school: State University
        location: City, ST
        degree: B.S. in Computer Science
        concentration: AI/ML
        minor: Math
        gpa: 3.7
        dates: "2021-08 / 2025-05"
        honors: [Dean's List, Honors College]
    experience:
      - org: Big Co
        title: Engineer
        location: City, ST
        dates: "2024-06 / 2024-08"
        achievements:
          - {id: bigco_a, what: "did a thing", angles: [x]}
          - {id: bigco_b, what: "did another", angles: [y]}
      - org: Side Gig
        title: Clerk
        location: City, ST
        dates: "2023-06 / 2024-01"
        achievements:
          - {id: gig_a, what: "served customers", angles: [ops]}
          - {id: gig_b, what: "trained hires", angles: [ops]}
          - {id: gig_c, what: "extra atom", angles: [ops]}
    projects:
      - name: ProjOne
        dates: "2024-01 / 2024-05"
        achievements:
          - {id: p1, what: "built an app", angles: [llm]}
      - name: ProjTwo
        dates: "2024-06 / 2024-09"
        achievements:
          - {id: p2a, what: "atom a", angles: [llm]}
          - {id: p2b, what: "atom b", angles: [llm]}
          - {id: p2c, what: "atom c", angles: [llm]}
          - {id: p2d, what: "atom d", angles: [llm]}
    leadership:
      - org: Club A
        dates: "2023-09 / 2024-05"
        achievements:
          - {id: la_a, what: "led members", angles: [lead]}
          - {id: la_b, what: "ran events", angles: [lead]}
      - org: Club B
        dates: "2022-09 / 2023-05"
        achievements:
          - {id: lb_a, what: "single atom org", angles: [lead]}
    skills:
      languages: [Python, SQL]
      developer_tools: [Git, Docker]
      frameworks: [Flask]
      libraries: [pandas]
""")


@pytest.fixture()
def synthetic_master(tmp_path, monkeypatch):
    p = tmp_path / "master.yaml"
    p.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    monkeypatch.delenv("RESUME_TAILOR_CANDIDATE", raising=False)
    for fn in _CACHED:
        fn.cache_clear()
    yield p
    for fn in _CACHED:
        fn.cache_clear()


def test_required_defaults_to_all_blocks(synthetic_master):
    req = compose._required_blocks()
    assert req["experience"] == ["Big Co", "Side Gig"]
    assert req["leadership"] == ["Club A", "Club B"]


def test_required_rejects_unknown_name(synthetic_master, monkeypatch):
    monkeypatch.setattr(assets, "tailor_config",
                        lambda: {"required": {"experience": ["Nonexistent"]}})
    with pytest.raises(RuntimeError, match="not in master_experience"):
        compose._required_blocks()


def test_required_accepts_bare_string(synthetic_master, monkeypatch):
    # A single block name written as a scalar must become a one-element list,
    # not iterate its characters.
    monkeypatch.setattr(assets, "tailor_config",
                        lambda: {"required": {"experience": "Big Co"}})
    assert compose._required_blocks()["experience"] == ["Big Co"]


def test_atom_material_len_reflects_content(synthetic_master):
    # An atom with impact bullets carries more grounded material than a bare one.
    assert compose.atom_material_len(["bigco_a"]) > 0
    assert compose.atom_material_len(["bigco_a", "bigco_b"]) > compose.atom_material_len(["bigco_a"])


def test_enforce_fixed_counts_pins_bullets(synthetic_master, monkeypatch):
    # Side Gig has 3 atoms but a [2,1] config -> exactly 2 bullet groups.
    # New contract: counts come from config.block_targets (config.json), not yaml.
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_layout": {
        "Side Gig": {"line_targets": [2, 1]},
        "Big Co": {"line_targets": [2, 2]},
        "Club A": {"line_targets": [1, 1]},
        "Club B": {"line_targets": [2]},
    }})
    sel = {
        "experience": [
            {"name": "Big Co", "groups": [["bigco_a"], ["bigco_b"]]},
            {"name": "Side Gig", "groups": [["gig_a"], ["gig_b"], ["gig_c"]]},
        ],
        "leadership": [
            {"name": "Club A", "groups": [["la_a"], ["la_b"]]},
            {"name": "Club B", "groups": [["lb_a"]]},
        ],
        "projects": [{"name": "ProjOne", "groups": [["p1"]]}],
    }
    compose._enforce_fixed_counts(sel)
    side = next(e for e in sel["experience"] if e["name"] == "Side Gig")
    assert len(side["groups"]) == 2
    # Big Co has 2-bullet config -> untouched.
    big = next(e for e in sel["experience"] if e["name"] == "Big Co")
    assert len(big["groups"]) == 2


def test_header_and_education_render_from_yaml(synthetic_master):
    tex = render.render(
        {"experience": [], "projects": [], "leadership": []}, {}, []
    )
    assert "Jane Q. Public" in tex
    assert "jane@example.com" in tex
    assert "State University" in tex
    assert "3.7 GPA" in tex
    assert "B.S. in Computer Science with a Concentration in AI/ML, Minor in Math" in tex
    assert "Awards \\& Honors:" in tex
    # the preamble is reused; no other person's data leaks in
    assert "\\begin{document}" in tex


def test_education_gpa_and_spacing(synthetic_master):
    tex = render.render({"experience": [], "projects": [], "leadership": []}, {}, [])
    # real GPA shown; \vspace{2pt} present even with honors absent
    assert "3.7 GPA" in tex
    assert "\\vspace{2pt}" in tex


def test_education_hides_zero_gpa(synthetic_master, monkeypatch):
    master = assets.load_master()
    master["education"][0]["gpa"] = 0
    monkeypatch.setattr(assets, "load_master", lambda: master)
    tex = render.render({"experience": [], "projects": [], "leadership": []}, {}, [])
    assert "0 GPA" not in tex
    assert "State University" in tex  # school still rendered, just no GPA


def test_candidate_slug_from_basics(synthetic_master):
    assert output.candidate_slug() == "Jane_Q._Public"


def test_candidate_slug_env_override(synthetic_master, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Custom_Name")
    assert output.candidate_slug() == "Custom_Name"


def test_project_targets_none_when_unconfigured(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.project_targets("ProjOne") is None


def test_project_targets_returns_configured(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": [3, 2, 1]}}})
    assert config.project_targets("ProjOne") == [3, 2, 1]


def test_project_targets_clamps_and_truncates(synthetic_master, monkeypatch):
    # ints clamped to 1-3, list truncated to 5
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": [9, 0, 2, 2, 2, 2]}}})
    assert config.project_targets("ProjOne") == [3, 1, 2, 2, 2]


def test_project_targets_bad_value_returns_none(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": ["x"]}}})
    assert config.project_targets("ProjOne") is None


def test_project_targets_empty_list_returns_none(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": []}}})
    assert config.project_targets("ProjOne") is None


def test_cap_projects_honors_per_project_count(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": [2, 2, 1]}}})  # 3 bullets
    clean = {"projects": [{"name": "ProjOne",
                           "groups": [["a"], ["b"], ["c"], ["d"]]}]}
    compose._cap_projects(clean)
    assert len(clean["projects"][0]["groups"]) == 3


def test_cap_projects_falls_back_to_global(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "PROJECT_BULLETS_MAX", 2)
    clean = {"projects": [{"name": "ProjOne", "groups": [["a"], ["b"], ["c"]]}]}
    compose._cap_projects(clean)
    assert len(clean["projects"][0]["groups"]) == 2


# --- projects honor their configured count: pad UP, not just cap (cycle 24) -------

def test_cap_projects_pads_configured_project_up_from_unused_atoms(synthetic_master, monkeypatch):
    # ProjTwo has 4 atoms; configured for 3 bullets but select() returned only 1 group.
    # _cap_projects must pad it UP to 3 from its OWN unused atoms (like experience does),
    # not leave it short — even though the page has room.
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjTwo": {"line_targets": [2, 2, 1]}}})  # 3 bullets
    clean = {"experience": [], "leadership": [],
             "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    compose._cap_projects(clean)
    groups = clean["projects"][0]["groups"]
    assert len(groups) == 3                              # padded 1 -> 3
    flat = [a for g in groups for a in g]
    assert flat[0] == "p2a"                              # original group kept, first, in order
    assert set(flat) <= {"p2a", "p2b", "p2c", "p2d"}     # padded only from ProjTwo's own atoms
    assert len(flat) == len(set(flat))                   # no atom reused


def test_cap_projects_preserves_fused_group_when_padding(synthetic_master, monkeypatch):
    # A fused (multi-atom) group select() chose is preserved intact; padding only
    # appends single-atom groups around it (singles=False semantics).
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjTwo": {"line_targets": [2, 2, 1]}}})  # 3 bullets
    clean = {"experience": [], "leadership": [],
             "projects": [{"name": "ProjTwo", "groups": [["p2a", "p2b"]]}]}
    compose._cap_projects(clean)
    groups = clean["projects"][0]["groups"]
    assert len(groups) == 3
    assert groups[0] == ["p2a", "p2b"]                   # fused group untouched
    assert all(len(g) == 1 for g in groups[1:])          # padding added singles


def test_cap_projects_unconfigured_project_not_padded(synthetic_master, monkeypatch):
    # No per-project layout -> cap-only behavior, unchanged: a short project is NOT
    # padded up (the engine only pads projects the user explicitly configured).
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "PROJECT_BULLETS_MAX", 2)
    clean = {"experience": [], "leadership": [],
             "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    compose._cap_projects(clean)
    assert clean["projects"][0]["groups"] == [["p2a"]]   # untouched, never padded


def test_cap_projects_best_effort_when_atoms_exhausted(synthetic_master, monkeypatch):
    # ProjOne has only 1 atom but is configured for 3 bullets: padding is best-effort
    # from the project's OWN atoms, so it stays at 1 group (no crash, no foreign atoms).
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": [2, 2, 1]}}})  # 3 bullets
    clean = {"experience": [], "leadership": [],
             "projects": [{"name": "ProjOne", "groups": [["p1"]]}]}
    compose._cap_projects(clean)
    assert clean["projects"][0]["groups"] == [["p1"]]    # only its own atom; nothing borrowed


def test_project_guidance_names_each_project_and_count(synthetic_master, monkeypatch):
    # The select() prompt guidance lists every project with its target bullet count:
    # configured projects use config.project_targets; unconfigured use the global default.
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjTwo": {"line_targets": [2, 2, 1]}}})  # 3
    monkeypatch.setattr(config, "PROJECT_BULLETS_MAX", 2)
    g = compose._project_guidance()
    assert "ProjTwo" in g and "3 bullet group" in g       # configured count
    assert "ProjOne" in g and "2 bullet group" in g       # unconfigured -> global default


def test_select_prompt_includes_project_guidance(synthetic_master, monkeypatch):
    # select() feeds the per-project guidance into its prompt and drops the old blanket
    # "weaker ones ~1 group" instruction that caused configured projects to under-fill.
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjTwo": {"line_targets": [2, 2, 1]}}})
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["user"] = user
        return {"experience": [], "projects": [], "leadership": [],
                "skill_focus": "general", "skills": {}, "rationale": ""}

    monkeypatch.setattr(compose, "call", fake_call)
    compose.select("Build data pipelines.", "Data Analyst", "ACME")
    assert "ProjTwo" in captured["user"] and "bullet group" in captured["user"]
    assert "weaker ones ~1 group" not in captured["user"]


def test_bullet_line_targets_honors_per_project(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_layout": {"ProjOne": {"line_targets": [3, 1]}}})
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjOne", "groups": [["a1"], ["a2"], ["a3"]]}]}
    targets = compose.bullet_line_targets(sel)
    # bullet0->3, bullet1->1, bullet2-> last(1)
    assert sorted(targets.values()) == [1, 1, 3]


def test_bullet_line_targets_project_fallback(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjOne", "groups": [["a1"], ["a2"]]}]}
    targets = compose.bullet_line_targets(sel)
    assert sorted(targets.values()) == [2, 2]


# --- master on/off toggle for the custom layout ----------------------------------

def test_resume_layout_enabled_defaults_true_when_absent(monkeypatch):
    # Absent flag = enabled, so existing configs keep their behavior.
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.resume_layout_enabled() is True


def test_disabled_toggle_ignores_both_layout_maps(monkeypatch):
    # With the toggle OFF the engine ignores BOTH layout maps and uses its
    # built-in defaults -- the saved targets stay on disk but don't apply.
    monkeypatch.setattr(config, "_config_json", lambda: {
        "resume_layout_enabled": False,
        "resume_layout": {"Example Corp": {"line_targets": [1, 1]}},
        "project_layout": {"ProjOne": {"line_targets": [3, 2, 1]}}})
    assert config.resume_layout_enabled() is False
    assert config.resume_layout() == {}
    assert config.project_layout() == {}
    assert config.block_targets("Example Corp") == config.DEFAULT_LINE_TARGETS
    assert config.project_targets("ProjOne") is None


def test_enabled_toggle_reads_both_layout_maps(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "resume_layout_enabled": True,
        "resume_layout": {"Example Corp": {"line_targets": [1, 1]}},
        "project_layout": {"ProjOne": {"line_targets": [3, 2, 1]}}})
    assert config.block_targets("Example Corp") == [1, 1]
    assert config.project_targets("ProjOne") == [3, 2, 1]
