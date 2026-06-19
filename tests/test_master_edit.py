"""Tests for the comment-preserving master_experience.yaml writer.

Uses a synthetic, commented YAML in tmp_path (the real file is gitignored personal
data) and monkeypatches config.MASTER_YAML at it.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, config, master_edit  # noqa: E402

_CACHED = (assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks)

_MASTER = textwrap.dedent("""\
    # HEADER COMMENT - must survive
    basics:
      name: Jane Q. Public
    experience:
      # an experience comment
      - org: Big Co
        title: Engineer
        location: City, ST
        dates: "2024-06 / 2024-08"
        achievements:
          - id: bigco_a
            what: "did a thing"
            angles: [x]
    projects:
      - name: ProjOne
        dates: "2024-01 / 2024-05"
        achievements:
          - id: p1
            what: "built an app"
            angles: [llm]
    leadership:
      - org: Club A
        dates: "2023-09 / 2024-05"
        achievements:
          - id: la_a
            what: "led members"
            angles: [lead]
    skills:
      languages: [Python]
""")


@pytest.fixture()
def master(tmp_path, monkeypatch):
    p = tmp_path / "master.yaml"
    p.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    for fn in _CACHED:
        fn.cache_clear()
    yield p
    for fn in _CACHED:
        fn.cache_clear()


def test_append_project(master):
    master_edit.append_entry("projects", {
        "name": "New Proj", "dates": "2025-01 / 2025-05",
        "achievements": [{"what": "did X", "angles": ["llm"]}],
    })
    assert "New Proj" in [p["name"] for p in assets.blocks()["projects"]]
    ids = set(assets.atoms_by_id())
    assert "new_proj_1" in ids
    assert "p1" in ids  # existing entry preserved


def test_append_experience(master):
    master_edit.append_entry("experience", {
        "org": "Acme", "title": "Dev", "location": "Remote",
        "dates": "2025-01 / 2025-06",
        "achievements": [{"what": "shipped Y", "angles": ["backend"]}],
    })
    assert "Acme" in [e["name"] for e in assets.blocks()["experience"]]
    assert "acme_1" in assets.atoms_by_id()


def test_append_leadership(master):
    master_edit.append_entry("leadership", {
        "org": "Honor Society", "dates": "2025",
        "achievements": [{"what": "led", "angles": ["lead"]}],
    })
    assert "Honor Society" in [e["name"] for e in assets.blocks()["leadership"]]
    assert "honor_society_1" in assets.atoms_by_id()


def test_comments_preserved(master):
    master_edit.append_entry("projects", {
        "name": "New Proj", "dates": "2025",
        "achievements": [{"what": "did X", "angles": ["llm"]}],
    })
    text = master.read_text(encoding="utf-8")
    assert "# HEADER COMMENT - must survive" in text
    assert "# an experience comment" in text


def test_unique_id_collision_suffix(master):
    def data():
        return {"name": "Dup", "dates": "2025",
                "achievements": [{"what": "x", "angles": ["a"]}]}
    master_edit.append_entry("projects", data())
    master_edit.append_entry("projects", data())  # would dup 'dup_1' without suffixing
    ids = set(assets.atoms_by_id())
    assert "dup_1" in ids
    assert "dup_1_x" in ids


def test_impact_normalized(master):
    master_edit.append_entry("projects", {
        "name": "WithImpact", "dates": "2025",
        "achievements": [{"what": "x", "angles": ["a"], "impact": ["big win", "  "]}],
    })
    atom = assets.atoms_by_id()["withimpact_1"]
    assert list(atom["impact"]) == ["big win"]  # blank impact dropped


def test_multi_achievement_ids(master):
    master_edit.append_entry("projects", {
        "name": "Multi Proj", "dates": "2025",
        "achievements": [
            {"what": "first thing", "angles": ["a"]},
            {"what": "second thing", "angles": ["b"]},
        ],
    })
    ids = set(assets.atoms_by_id())
    assert "multi_proj_1" in ids
    assert "multi_proj_2" in ids


@pytest.mark.parametrize("section,data,msg", [
    ("projects", {"name": "", "dates": "2025",
                  "achievements": [{"what": "x", "angles": ["a"]}]}, "name is required"),
    ("experience", {"org": "", "title": "t", "location": "l", "dates": "2025",
                    "achievements": [{"what": "x", "angles": ["a"]}]}, "org is required"),
    ("projects", {"name": "P", "dates": "",
                  "achievements": [{"what": "x", "angles": ["a"]}]}, "dates is required"),
    ("projects", {"name": "P", "dates": "2025", "achievements": []},
     "at least one achievement"),
    ("projects", {"name": "P", "dates": "2025",
                  "achievements": [{"what": "", "angles": ["a"]}]}, "needs a 'what'"),
    ("projects", {"name": "P", "dates": "2025",
                  "achievements": [{"what": "x", "angles": []}]}, "needs at least one angle"),
    ("bogus", {"name": "P", "dates": "2025",
               "achievements": [{"what": "x", "angles": ["a"]}]}, "unknown section"),
])
def test_validation_raises(master, section, data, msg):
    with pytest.raises(ValueError, match=msg):
        master_edit.append_entry(section, data)
