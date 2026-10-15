"""Bullets by strength can size each bullet: a tier's `lines` ("1:2-2-1") gives the
project at that strength rank one bullet per entry, each aimed at that many printed
lines. A tier with only a count ("2:3") behaves as before."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402
from qt import resume_data_tab as rdt  # noqa: E402
from resume_tailor import compose, config, selection  # noqa: E402
from resume_tailor.common import _gkey  # noqa: E402

TIERS = [{"projects": 1, "bullets": 3, "lines": [2, 2, 2]},
         {"projects": 1, "bullets": 3, "lines": [2, 2, 1]},
         {"projects": 1, "bullets": 2}]


def _sel():
    return {"experience": [], "leadership": [], "projects": [
        {"name": "P1", "groups": [["e1"], ["e2"], ["e3"]]},
        {"name": "P2", "groups": [["f1"], ["f2"], ["f3"]]},
        {"name": "P3", "groups": [["g1"], ["g2"]]},
    ]}


def _targets(sel):
    tgt = compose.bullet_line_targets(sel)
    return {e["name"]: [tgt[_gkey(g)] for g in e["groups"]] for e in sel["projects"]}


def test_rank_lines_and_counts(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"project_bullet_tiers": TIERS})
    assert config.project_bullet_tiers() == [3, 3, 2]
    assert config.project_rank_lines(0) == [2, 2, 2]
    assert config.project_rank_lines(1) == [2, 2, 1]
    assert config.project_rank_lines(2) is None      # a count-only tier
    assert config.project_rank_lines(3) is None      # past the last tier


def test_lines_set_the_count_and_are_clamped(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"project_bullet_tiers": [
        {"projects": 1, "bullets": 5, "lines": [9, 0]},           # lines win over bullets
        {"projects": 1, "bullets": 2, "lines": ["x"]}]})          # bad lines -> the count
    assert config.project_bullet_tiers() == [2, 2]
    assert config.project_rank_lines(0) == [3, 1]
    assert config.project_rank_lines(1) is None


def test_lines_follow_the_master_toggle(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "resume_layout_enabled": False, "project_bullet_tiers": TIERS})
    assert config.project_rank_lines(1) is None


def test_second_projects_last_bullet_is_one_line(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"project_bullet_tiers": TIERS})
    assert _targets(_sel()) == {"P1": [2, 2, 2], "P2": [2, 2, 1], "P3": [2, 2]}


def test_the_rank_decides_not_the_name(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"project_bullet_tiers": TIERS})
    sel = _sel()
    sel["projects"][:2] = sel["projects"][1::-1]      # P2 is strongest for this job
    assert _targets(sel)["P1"] == [2, 2, 1]


def test_a_per_project_box_still_wins(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {
        "project_bullet_tiers": TIERS,
        "project_layout": {"P2": {"line_targets": [3, 3, 3]}}})
    assert _targets(_sel())["P2"] == [3, 3, 3]


def test_cap_projects_pads_to_the_lines_count(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_PROJECTS_MAX", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"project_bullet_tiers": TIERS})
    atoms = {"P1": ["e1", "e2", "e3"], "P2": ["f1", "f2", "f3"], "P3": ["g1", "g2"]}
    monkeypatch.setattr(selection, "_block_atoms", lambda section, name: atoms[name])
    sel = _sel()
    sel["projects"][1]["groups"] = [["f1"]]
    compose._cap_projects(sel)
    assert sel["projects"][1]["groups"] == [["f1"], ["f2"], ["f3"]]


def test_jobsdata_keeps_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_bullet_tiers([
        {"projects": 1, "bullets": 9, "lines": [2, 5, 1]},
        {"projects": 2, "bullets": 2}])
    assert jobsdata.load_project_bullet_tiers() == [
        {"projects": 1, "bullets": 3, "lines": [2, 3, 1]},
        {"projects": 2, "bullets": 2}]


def test_box_text_round_trips():
    tiers = rdt._parse_tiers("1:2-2-2, 1:2 2 1; 1:2")
    assert tiers == [{"projects": 1, "bullets": 3, "lines": [2, 2, 2]},
                     {"projects": 1, "bullets": 3, "lines": [2, 2, 1]},
                     {"projects": 1, "bullets": 2}]
    assert rdt._tiers_to_text(tiers) == "1:2-2-2, 1:2-2-1, 1:2"


def test_box_still_drops_junk():
    assert rdt._parse_tiers("2:3, 1:x-2, junk, 1:") == [{"projects": 2, "bullets": 3}]
