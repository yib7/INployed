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

from resume_tailor import assets, compose, config, measure, output, render  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

_CACHED = (
    assets.load_master, assets.tailor_config, assets.atoms_by_id,
    assets.blocks, assets.template_head, assets.skill_aliases,
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
      concepts_and_methodologies:
        - "A/B Testing"
        - "Feature Engineering"
        - "Hypothesis Testing"
        - "Exploratory Data Analysis (EDA)"
        - "Data Cleaning & Preprocessing"
    skill_aliases:
      "A/B Testing": ["Experimentation", "Split Testing"]
      "Data Cleaning & Preprocessing": ["Data Wrangling"]
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


# --- fill underfull bullets from unused SAME-block atoms --------------------------
# When a tailored bullet renders shorter than its configured line target and the page
# has room, fold ONE detail from an unused atom in the SAME block in to fill it. Never
# fabricates: a bullet whose block has no spare atom is left exactly as-is. Implemented
# as group-augmentation (append the borrowed id to the group, re-key the bullet) so
# render / targets / trace all key off the same atom ids.

def _fake_bullets(*pairs):
    """Build a compose.call stub returning {"bullets":[{gkey,text}, ...]} verbatim."""
    def _call(system, user, tier, **kw):
        return {"bullets": [{"gkey": gk, "text": txt} for gk, txt in pairs]}
    return _call


def test_fill_underfull_fuses_unused_block_atom(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})   # ProjTwo unconfigured -> 2-line target
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    bullets = {"p2a": "Built an app."}                        # stubby -> underfull at 2 lines
    longer = "Built an app, folding in a second grounded detail to fill out the line nicely."
    monkeypatch.setattr(compose, "call", _fake_bullets(("p2a", longer)))
    out = compose.fill_underfull("a long job description " * 8, "Engineer", sel, bullets)
    # the group gained the first spare atom (p2b) and the bullet was re-keyed
    assert sel["projects"][0]["groups"] == [["p2a", "p2b"]]
    assert "p2a" not in out
    assert out["p2a+p2b"] == longer


def test_fill_underfull_skips_block_with_no_spare_atom(synthetic_master, monkeypatch):
    # ProjOne has a single atom (p1): no spare in-block -> bullet left as-is, NO LLM call.
    monkeypatch.setattr(config, "_config_json", lambda: {})

    def boom(*a, **k):
        raise AssertionError("the LLM must not be called when there is no spare atom")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjOne", "groups": [["p1"]]}]}
    bullets = {"p1": "Built an app."}
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {"p1": "Built an app."}
    assert sel["projects"][0]["groups"] == [["p1"]]


def test_fill_underfull_skips_already_full_bullet(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})

    def boom(*a, **k):
        raise AssertionError("a full bullet must not be sent to the LLM")

    monkeypatch.setattr(compose, "call", boom)
    full = ("word " * 80).strip()                            # width well past the 2-line floor
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    bullets = {"p2a": full}
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {"p2a": full}
    assert sel["projects"][0]["groups"] == [["p2a"]]


def test_fill_underfull_skips_group_at_max_atoms(synthetic_master, monkeypatch):
    # A group already fusing 3 atoms is never extended, even if stubby.
    monkeypatch.setattr(config, "_config_json", lambda: {})

    def boom(*a, **k):
        raise AssertionError("a 3-atom group must not be extended")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a", "p2b", "p2c"]]}]}
    bullets = {"p2a+p2b+p2c": "Built an app."}
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {"p2a+p2b+p2c": "Built an app."}
    assert sel["projects"][0]["groups"] == [["p2a", "p2b", "p2c"]]


def test_fill_underfull_never_folds_one_atom_into_two_bullets(synthetic_master, monkeypatch):
    # Two underfull bullets in one block must borrow DIFFERENT spare atoms.
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"], ["p2b"]]}]}
    bullets = {"p2a": "Built an app.", "p2b": "Made a tool."}
    monkeypatch.setattr(compose, "call", _fake_bullets(
        ("p2a", "Built an app, with one more grounded detail to fill out the printed line."),
        ("p2b", "Made a tool, with one more grounded detail to fill out the printed line."),
    ))
    compose.fill_underfull("jd", "Engineer", sel, bullets)
    groups = sel["projects"][0]["groups"]
    assert groups[0] == ["p2a", "p2c"]                       # first spare
    assert groups[1] == ["p2b", "p2d"]                       # a DIFFERENT spare
    assert "p2a+p2c" in bullets and "p2b+p2d" in bullets


def test_fill_underfull_leaves_bullet_when_model_returns_unchanged(synthetic_master, monkeypatch):
    # The model declined to fold anything in (returned identical text) -> bullet + group
    # untouched and the spare atom is released (stays unused).
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    bullets = {"p2a": "Built an app."}
    monkeypatch.setattr(compose, "call", _fake_bullets(("p2a", "Built an app.")))
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {"p2a": "Built an app."}
    assert sel["projects"][0]["groups"] == [["p2a"]]


def test_fill_underfull_best_effort_on_llm_failure(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    bullets = {"p2a": "Built an app."}
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {"p2a": "Built an app."}                   # advisory: unchanged on failure
    assert sel["projects"][0]["groups"] == [["p2a"]]


def test_fill_then_trim_keeps_within_line_target(synthetic_master, monkeypatch):
    # An overshoot from the fill pass is trimmed back to the bullet's line target.
    monkeypatch.setattr(config, "_config_json", lambda: {})
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"]]}]}
    bullets = {"p2a": "Built an app."}
    overshoot = "Built an app " + "with a great many extra grounded words " * 10
    monkeypatch.setattr(compose, "call", _fake_bullets(("p2a", overshoot)))
    compose.fill_underfull("jd", "Engineer", sel, bullets)
    rt_run._trim_to_caps(sel, bullets)
    assert "p2a+p2b" in bullets
    assert measure.line_count(bullets["p2a+p2b"]) <= 2


def test_fill_underfull_skips_verbatim_bullets(synthetic_master, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})

    def boom(*a, **k):
        raise AssertionError("verbatim bullets must never be touched")

    monkeypatch.setattr(compose, "call", boom)
    gk = "__verbatim__/ProjTwo/0"
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [[gk]]}]}
    bullets = {gk: "Short verbatim bullet."}
    out = compose.fill_underfull("jd", "Engineer", sel, bullets)
    assert out == {gk: "Short verbatim bullet."}


# --- config gate: RESUME_TAILOR_FILL_UNDERFULL ----------------------------------

def test_fill_underfull_enabled_default_true(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_FILL_UNDERFULL", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.fill_underfull_enabled() is True


def test_fill_underfull_enabled_env_off(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_FILL_UNDERFULL", "0")
    assert config.fill_underfull_enabled() is False


def test_fill_underfull_enabled_config_off(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_FILL_UNDERFULL", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"fill_underfull": False})
    assert config.fill_underfull_enabled() is False


# --- lead-with-overview: project bullets lead with the "what is this" overview --------
# select() orders a project's bullet GROUPS purely by JD-relevance, so a project's
# overview ("what is this project at a glance") can land on bullet 2 or 3 behind detail
# bullets — the reader hits the tech history before learning what the thing IS.
# lead_with_overview() floats the intro bullet to the front (a cheap model pass picks it;
# a deterministic file-order fallback — the master authors the overview atom first —
# guarantees flow even with no/failed model call). Projects only; pure reorder, no invent.

def test_lead_with_overview_floats_model_pick_to_front(synthetic_master, monkeypatch):
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"], ["p2b"], ["p2c"]]}]}
    # The model designates bullet 3 (p2c) as the overview/intro.
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: {"projects": [{"project": "ProjTwo", "lead": 3}]})
    compose.lead_with_overview("a real job description " * 5, "Engineer", sel)
    assert sel["projects"][0]["groups"] == [["p2c"], ["p2a"], ["p2b"]]   # rest keep order


def test_lead_with_overview_falls_back_to_file_order_on_failure(synthetic_master, monkeypatch):
    # Model call fails -> deterministic fallback floats the group holding the earliest
    # AUTHORED atom (p2a, file-order index 0) to the front, leaving the rest in place.
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2c"], ["p2a"], ["p2b"]]}]}
    compose.lead_with_overview("jd", "Engineer", sel)
    assert sel["projects"][0]["groups"] == [["p2a"], ["p2c"], ["p2b"]]


def test_lead_with_overview_invalid_pick_uses_file_order(synthetic_master, monkeypatch):
    # An out-of-range pick is ignored and the file-order fallback applies.
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: {"projects": [{"project": "ProjTwo", "lead": 99}]})
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [["p2c"], ["p2a"]]}]}
    compose.lead_with_overview("jd", "Engineer", sel)
    assert sel["projects"][0]["groups"] == [["p2a"], ["p2c"]]


def test_lead_with_overview_single_bullet_project_makes_no_call(synthetic_master, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no LLM call when there is nothing to reorder")

    monkeypatch.setattr(compose, "call", boom)
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjOne", "groups": [["p1"]]}]}
    compose.lead_with_overview("jd", "Engineer", sel)
    assert sel["projects"][0]["groups"] == [["p1"]]


def test_lead_with_overview_skips_verbatim_project(synthetic_master, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a verbatim project's order is the user's — never reorder it")

    monkeypatch.setattr(compose, "call", boom)
    groups = [["__verbatim__/ProjTwo/0"], ["__verbatim__/ProjTwo/1"]]
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "ProjTwo", "groups": [g[:] for g in groups]}]}
    compose.lead_with_overview("jd", "Engineer", sel)
    assert sel["projects"][0]["groups"] == groups


def test_lead_with_overview_leaves_experience_and_leadership(synthetic_master, monkeypatch):
    # Only projects are reordered; experience/leadership keep their template/relevance order
    # even when a project triggers the model call.
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: {"projects": [{"project": "ProjTwo", "lead": 1}]})
    sel = {"experience": [{"name": "Big Co", "groups": [["bigco_a"], ["bigco_b"]]}],
           "leadership": [{"name": "Club A", "groups": [["la_a"], ["la_b"]]}],
           "projects": [{"name": "ProjTwo", "groups": [["p2a"], ["p2b"]]}]}
    compose.lead_with_overview("jd", "Engineer", sel)
    assert sel["experience"][0]["groups"] == [["bigco_a"], ["bigco_b"]]
    assert sel["leadership"][0]["groups"] == [["la_a"], ["la_b"]]


def test_select_prompt_leads_projects_with_overview(synthetic_master, monkeypatch):
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["user"] = user
        return {"experience": [], "projects": [], "leadership": [],
                "skill_focus": "general", "skills": {}, "rationale": ""}

    monkeypatch.setattr(compose, "call", fake_call)
    compose.select("Build data pipelines.", "Data Analyst", "ACME")
    assert "overview" in captured["user"].lower()


# --- config gate: RESUME_TAILOR_LEAD_OVERVIEW -----------------------------------

def test_lead_overview_enabled_default_true(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_LEAD_OVERVIEW", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.lead_overview_enabled() is True


def test_lead_overview_enabled_env_off(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_LEAD_OVERVIEW", "0")
    assert config.lead_overview_enabled() is False


def test_lead_overview_enabled_config_off(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_LEAD_OVERVIEW", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"lead_overview": False})
    assert config.lead_overview_enabled() is False


# --- Methods line: select() methods ranking is cleaned/anchored to the pool -----

def test_normalize_selection_cleans_methods_to_pool(synthetic_master):
    sel = compose._normalize_selection({
        "experience": [], "projects": [], "leadership": [],
        "methods": ["A/B Testing", "a/b testing", "Totally Invented", "Feature Engineering"],
    })
    # deduped (case-insensitive), invented dropped, printed in the pool's spelling
    assert sel["methods"] == ["A/B Testing", "Feature Engineering"]


def test_normalize_selection_methods_defaults_empty(synthetic_master):
    sel = compose._normalize_selection({"experience": [], "projects": [], "leadership": []})
    assert sel["methods"] == []


# --- Methods line: the two-tier builder ----------------------------------------

def test_methods_line_tier1_prints_jd_spelling_ranked_by_frequency(synthetic_master):
    # alias-only hit -> the JD's spelling ('Experimentation'); direct hit -> the concept.
    jd = "We love Experimentation, Experimentation, Experimentation. Some Feature Engineering too."
    line = compose.methods_line(jd, {"methods": []})
    items = line["items"].split(", ")
    assert "Experimentation" in items
    assert "Feature Engineering" in items
    assert "A/B Testing" not in items                 # canonical not printed when the JD uses the alias
    assert items.index("Experimentation") < items.index("Feature Engineering")  # by JD frequency


def test_methods_line_tier1_ties_broken_by_model_relevance(synthetic_master):
    # Two equal-frequency JD hits: the model ranks the alphabetically-LATER one higher,
    # so role-relevance (not the alphabet) decides which earned buzzword leads.
    jd = "We need Feature Engineering and A/B Testing."          # each appears once
    sel = {"methods": ["Feature Engineering", "A/B Testing"]}    # model: FE more relevant
    line = compose.methods_line(jd, sel)
    items = line["items"].split(", ")
    assert items.index("Feature Engineering") < items.index("A/B Testing")


def test_methods_line_tier2_pads_from_model_ranking_dedup_by_canonical(synthetic_master):
    jd = "Experimentation everywhere."               # exactly one Tier-1 hit
    sel = {"methods": ["A/B Testing", "Hypothesis Testing", "Exploratory Data Analysis (EDA)"]}
    line = compose.methods_line(jd, sel)
    items = line["items"].split(", ")
    assert items[0] == "Experimentation"             # Tier-1 leads
    assert "A/B Testing" not in items                # same canonical as the chosen alias -> skipped
    assert "Hypothesis Testing" in items and "Exploratory Data Analysis (EDA)" in items


def test_methods_line_tier2_anchors_to_pool(synthetic_master):
    # a model 'method' that is not a real concept is never printed (anchored to the pool)
    line = compose.methods_line("nothing here", {"methods": ["Hypothesis Testing", "Invented Method"]})
    items = line["items"].split(", ")
    assert items == ["Hypothesis Testing"]


def test_methods_line_none_when_pool_empty(synthetic_master, monkeypatch):
    monkeypatch.setattr(compose, "_methods_pool", lambda: [])
    assert compose.methods_line("Experimentation", {"methods": ["Hypothesis Testing"]}) is None


def test_methods_line_none_when_target_zero(synthetic_master, monkeypatch):
    monkeypatch.setattr(compose.layout, "skill_targets", lambda: {"Methods": 0})
    assert compose.methods_line("Experimentation", {"methods": ["Hypothesis Testing"]}) is None


def test_methods_line_width_capped_to_one_line(synthetic_master, monkeypatch):
    monkeypatch.setattr(compose.layout, "skill_targets", lambda: {"Methods": 6})
    cap = compose.measure.skill_line_width("Methods", "Experimentation")
    monkeypatch.setattr(compose.measure, "SKILL_LINE_CAPACITY", cap)
    jd = "Experimentation and Feature Engineering and Hypothesis Testing"
    line = compose.methods_line(jd, {"methods": []})
    assert line["items"] == "Experimentation"        # only the first token fits the tiny capacity


# --- config gate: RESUME_TAILOR_METHODS_LINE ------------------------------------

def test_methods_line_enabled_default_true(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_METHODS_LINE", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.methods_line_enabled() is True


def test_methods_line_enabled_env_off(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_METHODS_LINE", "0")
    assert config.methods_line_enabled() is False


def test_methods_line_enabled_config_off(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_METHODS_LINE", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"methods_line": False})
    assert config.methods_line_enabled() is False


def test_methods_line_label_default(synthetic_master, monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_METHODS_LABEL", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.methods_line_label() == "Methods"
