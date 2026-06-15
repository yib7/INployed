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


def test_fixed_experience_specs_from_config(synthetic_master):
    specs = compose._fixed_experience_specs()
    assert specs == {"Side Gig": [2, 1]}


def test_enforce_fixed_counts_pins_bullets(synthetic_master):
    # Side Gig has 3 atoms but a [2,1] spec -> exactly 2 bullet groups.
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
    # Big Co is free -> untouched.
    big = next(e for e in sel["experience"] if e["name"] == "Big Co")
    assert len(big["groups"]) == 2


def test_layout_budgets_honor_config(synthetic_master):
    sel = {
        "experience": [{"name": "Side Gig", "groups": [["gig_a"], ["gig_b"]]}],
        "leadership": [
            {"name": "Club A", "groups": [["la_a"], ["la_b"]]},  # 2 atoms -> two 1-line
            {"name": "Club B", "groups": [["lb_a"]]},            # 1 atom -> one 2-line
        ],
    }
    budgets = compose.layout_budgets(sel)
    assert budgets["gig_a"] == 2 and budgets["gig_b"] == 1
    assert budgets["la_a"] == 1 and budgets["la_b"] == 1
    assert budgets["lb_a"] == 2


def test_header_and_education_render_from_yaml(synthetic_master):
    tex = render.render(
        {"experience": [], "projects": [], "leadership": []}, {}, []
    )
    assert "Jane Q. Public" in tex
    assert "jane@example.com" in tex
    assert "State University" in tex
    assert "3.7 GPA" in tex
    assert "B.S. in Computer Science with a Concentration in AI/ML, Minor in Math" in tex
    assert "Honors:" in tex
    # the preamble is reused; no other person's data leaks in
    assert "\\begin{document}" in tex


def test_candidate_slug_from_basics(synthetic_master):
    assert output.candidate_slug() == "Jane_Q._Public"


def test_candidate_slug_env_override(synthetic_master, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Custom_Name")
    assert output.candidate_slug() == "Custom_Name"
