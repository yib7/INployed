"""The tailor's Jev steps (TL-1 to TL-3) as `run.tailor()` meets them.

With Jev off the tailor must make exactly the LLM calls it made before cycle 19,
with byte-identical prompts. `test_jev_off_prompts_match_the_recording` pins that:
the prompts of the three stages the Jev steps sit beside (`select`, the
`lead_with_overview` ordering call and `compress_skills`' fallback call) and the
order and tier of every call a golden run makes were recorded from the engine
before the Jev wiring landed, into `tests/fixtures/tailor_jev_off_prompts.json`.
A prompt change made on purpose re-records the file (set TAILOR_JEV_OFF_PROMPTS_RECORD=1
for one run) and shows the diff in review; a change nobody meant fails here.

The rest covers each Jev step where its answer lands (the shortlist and the skills
in `select`, the lead in `lead_with_overview`).

The runs reuse the golden module's pinned engine (`test_tailor_golden.pinned_engine`),
so no model is ever reached: its stub raises on any prompt it does not know. Every
judge here is a fake, so no Jev request leaves the process either.
"""
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
import jev_switch  # noqa: E402
from resume_tailor import (apply_data, assets, compose, config, jev_assist, measure,  # noqa: E402
                           output, render, selection, skills)
from resume_tailor import run as rt_run  # noqa: E402
from resume_tailor.compile import CompileResult  # noqa: E402

import test_tailor_golden as golden  # noqa: E402 - sibling test module, no pkg import

# The golden module's fixtures, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

PROMPTS = REPO / "tests" / "fixtures" / "tailor_jev_off_prompts.json"
RECORD_ENV = "TAILOR_JEV_OFF_PROMPTS_RECORD"
# The stages whose prompts are pinned word for word: the ones a Jev step replaces
# or trims when Jev is on.
PINNED_STAGES = ("select", "lead_with_overview")

# compress_skills' fallback answer: any pool-backed lines will do, the prompt is
# what is recorded.
_FALLBACK_SKILLS = {"Languages": "Python, SQL", "Frameworks": "FastAPI",
                    "Developer Tools": "Git", "Libraries": "pandas"}


def _recording(stages, calls):
    """The golden stub, recording every call's prompts, tier and options."""
    stub = golden._make_stub(stages)

    def _call(system, user, tier, **kw):
        out = stub(system, user, tier, **kw)
        calls.append({"stage": stages[-1], "system": system, "user": user,
                      "tier": tier, "kw": kw})
        return out
    return _call


def _run_tailor(monkeypatch, tmp_path, job=None, **kw):
    """`run.tailor()` over the golden job, stubbed at the render seam exactly as
    `test_run_tailor_matches_the_golden` stubs it. Returns what reached the page."""
    captured: dict = {}

    def _fake_enforce(sel, bullets, skill_lines, tex_path, work_dir, jd="",
                      on_status=None, keep_projects=None):
        tex = render.render(sel, bullets, skill_lines)
        Path(tex_path).write_text(tex, encoding="utf-8")
        pdf = Path(work_dir) / "golden.pdf"
        pdf.write_bytes(b"%PDF-1.4 golden stub\n")
        captured.update(sel=sel, bullets=dict(bullets), skill_lines=list(skill_lines),
                        tex=tex)
        return CompileResult(True, pdf, ""), dict(bullets), tex

    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(rt_run, "pdflatex_available", lambda: True)
    monkeypatch.setattr(rt_run, "enforce_one_page", _fake_enforce)
    monkeypatch.setattr(output, "resolve_dir", lambda *a, **k: out)
    monkeypatch.setattr(apply_data, "write", lambda *a, **k: None)
    captured["out"] = rt_run.tailor(job or golden._JOB, ats_report=False, **kw)
    return captured


def _lines(text):
    return text.split("\n")


def _record_jev_off(monkeypatch, tmp_path):
    """Everything the recording pins, from one golden run plus one fallback call."""
    stages: list = []
    calls: list = []
    golden._install_stub(monkeypatch, _recording(stages, calls))
    _run_tailor(monkeypatch, tmp_path)

    fallback: dict = {}

    def _fallback_call(system, user, tier, **kw):
        fallback.update(system=system, user=user, tier=tier, kw=kw)
        return dict(_FALLBACK_SKILLS)

    monkeypatch.setattr(skills, "call", _fallback_call)
    compose.compress_skills(golden._JD, golden._JOB["job_title"],
                            {"skills": {}, "skill_focus": "data_analytics"})

    prompts = {c["stage"]: {"system": _lines(c["system"]), "user": _lines(c["user"])}
               for c in calls if c["stage"] in PINNED_STAGES}
    prompts["skills_fallback"] = {"system": _lines(fallback["system"]),
                                  "user": _lines(fallback["user"])}
    return {
        "calls": [{"stage": c["stage"], "tier": c["tier"], "kw": c["kw"]} for c in calls],
        "prompts": prompts,
        "skills_fallback_call": {"tier": fallback["tier"], "kw": fallback["kw"]},
    }


def _jev_off(monkeypatch):
    """Jev off for the tailor whatever the environment holds: the switch hands out
    no client, as it does with no TypeSafe key (the suite's default)."""
    monkeypatch.setattr(jev_switch, "client", lambda area: None)


def test_jev_off_prompts_match_the_recording(pinned_engine, stub_template_head,
                                             tmp_path, monkeypatch):
    """Jev off makes exactly the recorded calls, and the stages beside the Jev
    steps send the recorded prompts."""
    _jev_off(monkeypatch)
    got = _record_jev_off(monkeypatch, tmp_path)
    if os.environ.get(RECORD_ENV) == "1":
        PROMPTS.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
        pytest.skip(f"recorded {PROMPTS.name}; run again without {RECORD_ENV}")
    want = json.loads(PROMPTS.read_text(encoding="utf-8"))
    assert [c["stage"] for c in got["calls"]] == [c["stage"] for c in want["calls"]]
    assert got["calls"] == want["calls"]
    for stage in (*PINNED_STAGES, "skills_fallback"):
        assert got["prompts"][stage]["system"] == want["prompts"][stage]["system"], stage
        assert got["prompts"][stage]["user"] == want["prompts"][stage]["user"], stage
    assert got["skills_fallback_call"] == want["skills_fallback_call"]
    assert got == want


# ── fake judges ──────────────────────────────────────────────────────────────
class _Recording:
    """Wraps a judge and keeps every request it was sent."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        return self.inner.judge(state, questions)


class _Pick:
    """Answers every choice with option `choice` at `confidence`."""

    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence

    def judge(self, state, questions):
        return {qid: jev.Answer(kind="choice", choice=self.choice,
                                probabilities={n: float(n == self.choice) for n in q["criteria"]},
                                confidence=self.confidence)
                for qid, q in questions.items()}


class _Failing:
    def judge(self, state, questions):
        raise RuntimeError("the request was rejected")


# ── a master wide enough to trim ─────────────────────────────────────────────
def _atoms(prefix, n):
    return [{"id": f"{prefix}{i}", "what": f"Shipped the {prefix}{i} deliverable",
             "angles": ["work"]} for i in range(1, n + 1)]


_WIDE_MASTER = {
    "basics": {"name": "Jo Park", "email": "jo@example.com"},
    "experience": [
        {"org": "Wide Co", "title": "Analyst", "dates": "2024-01 / 2024-06",
         "achievements": _atoms("w", 8)},
        {"org": "Deep Co", "title": "Engineer", "dates": "2023-01 / 2023-06",
         "achievements": _atoms("d", 10)},
        {"org": "Tiny Co", "title": "Intern", "dates": "2022-06 / 2022-08",
         "achievements": _atoms("t", 2)},
    ],
    "projects": [{"name": f"P{k}", "dates": "2024-01 / 2024-05",
                  "achievements": _atoms(f"p{k}_", 2)} for k in range(1, 6)],
    "leadership": [{"org": "Club", "dates": "2022-09 / 2024-05",
                    "achievements": _atoms("c", 3)}],
    "skills": {"languages": ["Python", "SQL", "Go"], "frameworks": ["Flask"],
               "developer_tools": ["Git", "Docker"], "libraries": ["pandas"],
               "concepts_and_methodologies": ["ETL"]},
    "tailor": {"required": {"experience": ["Wide Co", "Tiny Co"]}},
}
_WIDE_CONFIG = {
    "resume_layout": {"Wide Co": {"line_targets": [2, 1]},
                      "Deep Co": {"line_targets": [3, 3, 3]},
                      "Tiny Co": {"line_targets": [1]},
                      "Club": {"line_targets": [1, 1]}},
    "project_layout": {"P1": {"line_targets": [2, 1]}},
    "projects_max": 2,
}
_WIDE_CACHED = (assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks,
                assets.skill_aliases, assets.skill_aliases_match_only)

# What Jev said about each atom of the wide master.
_RELEVANCE = {
    **dict(zip([f"w{i}" for i in range(1, 9)], (0.1, 0.9, 0.5, 0.5, 0.8, 0.2, 0.3, 0.7))),
    **{f"d{i}": 0.5 for i in range(1, 11)}, "d4": 0.1,
    "t1": 0.0, "t2": 0.0,
    "c1": 0.2, "c2": 0.6, "c3": 0.4,
    "p1_1": 0.3, "p1_2": 0.2, "p2_1": 0.1, "p2_2": 0.9, "p3_1": 0.4, "p3_2": 0.9,
    "p4_1": 0.2, "p4_2": 0.3, "p5_1": 0.1, "p5_2": 0.0,
}
# The shortlist that follows: per block, the most probable atoms first.
_SHORTLIST = {
    # 2 bullets: twice that plus two is 6 of its 8 atoms (w3 before w4: a tie keeps file order)
    "Wide Co": ["w2", "w5", "w8", "w3", "w4", "w7"],
    # 3 bullets of 3 lines: its layout needs 9 atoms, one more than twice 3 plus 2
    "Deep Co": ["d1", "d2", "d3", "d5", "d6", "d7", "d8", "d9", "d10"],
    "Tiny Co": ["t1", "t2"],                 # required, and every atom unlikely
    # projects by their best atoms: P3 (0.9, 0.4), P2 (0.9, 0.1), P1 and P4 tie at
    # (0.3, 0.2) and keep file order; the cap is 2, so 4 stay and P5 goes
    "P3": ["p3_2", "p3_1"], "P2": ["p2_2", "p2_1"], "P1": ["p1_1", "p1_2"],
    "P4": ["p4_2", "p4_1"],
    "Club": ["c2", "c3", "c1"],
}
_JD = "Analyst role: ship deliverables, write SQL and Python, report weekly."


@pytest.fixture()
def wide_master(tmp_path, monkeypatch):
    p = tmp_path / "master_experience.yaml"
    p.write_text(yaml.safe_dump(_WIDE_MASTER, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    monkeypatch.setattr(config, "_config_json", lambda: dict(_WIDE_CONFIG))
    for var in ("RESUME_TAILOR_PROJECTS_MAX", "RESUME_TAILOR_SKILL_TARGETS",
                "RESUME_TAILOR_TECH_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "PROJECT_BULLETS_MAX", 2)
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2, 2])
    for fn in _WIDE_CACHED:
        fn.cache_clear()
    yield p
    for fn in _WIDE_CACHED:
        fn.cache_clear()


def _select_prompts(monkeypatch, answer=None, **kw):
    """(system, user, sel) of one select() call with the model stubbed."""
    seen = {}

    def fake_call(system, user, tier, **opts):
        seen.update(system=system, user=user)
        return answer or {"experience": [], "projects": [], "leadership": [],
                          "skill_focus": "general", "skills": {}, "rationale": ""}

    monkeypatch.setattr(selection, "call", fake_call)
    sel = selection.select(_JD, "Analyst", "Acme", **kw)
    return seen["system"], seen["user"], sel


def _catalog_ids(user):
    return [line.split(":")[0].strip("- ").strip() for line in user.splitlines()
            if line.startswith("   - ")]


# ── TL-2: the shortlist select() sees ────────────────────────────────────────
def test_the_shortlist_keeps_each_blocks_most_probable_atoms(wide_master):
    got = selection._shortlist(_RELEVANCE)
    atoms = {b["name"]: b["atoms"] for sec in got.values() for b in sec}
    assert atoms == _SHORTLIST


def test_projects_are_ranked_by_their_best_atoms_and_capped(wide_master):
    got = selection._shortlist(_RELEVANCE)
    assert [b["name"] for b in got["projects"]] == ["P3", "P2", "P1", "P4"]


def test_every_experience_and_leadership_block_stays(wide_master):
    """Required blocks always stay: Tiny Co is required, and every atom it has is
    rated 0.0. The shortlist trims atoms inside a block; it drops only projects."""
    got = selection._shortlist(_RELEVANCE)
    assert [b["name"] for b in got["experience"]] == ["Wide Co", "Deep Co", "Tiny Co"]
    assert [b["name"] for b in got["leadership"]] == ["Club"]
    assert selection._required_blocks()["experience"] == ["Wide Co", "Tiny Co"]


def test_select_sees_the_trimmed_catalog_in_relevance_order(wide_master, monkeypatch):
    _system, user, _sel = _select_prompts(monkeypatch, atom_relevance=_RELEVANCE)
    assert _catalog_ids(user) == [a for name in ("Wide Co", "Deep Co", "Tiny Co", "P3", "P2",
                                                 "P1", "P4", "Club")
                                  for a in _SHORTLIST[name]]
    assert "[projects] P5" not in user and "P5:" not in user
    guidance = [line.split(":")[0].strip("- ").strip() for line in user.splitlines()
                if line.startswith("  - P")]
    assert guidance == ["P3", "P2", "P1", "P4"]


def test_select_without_jev_sends_the_whole_catalog(wide_master, monkeypatch):
    _system, user, _sel = _select_prompts(monkeypatch)
    ids = _catalog_ids(user)
    assert ids == list(assets.atoms_by_id())
    assert "[projects] P5" in user


def test_select_with_an_empty_rating_sends_the_whole_catalog(wide_master, monkeypatch):
    """No rating is today's path: an empty dict never trims the catalog to nothing."""
    _s1, plain, _ = _select_prompts(monkeypatch)
    _s2, rated, _ = _select_prompts(monkeypatch, atom_relevance={})
    assert rated == plain


# ── TL-1: the skills lines ───────────────────────────────────────────────────
_SKILL_PICK = {
    "lines": {"Languages": ["SQL", "Python", "Go"], "Frameworks": ["Flask"],
              "Developer Tools": ["Docker", "Git"], "Libraries": ["pandas"]},
    "skill_focus": "data_analytics",
    "probabilities": {"SQL": 0.9, "Python": 0.8, "Go": 0.1, "Flask": 0.3, "Docker": 0.6,
                      "Git": 0.5, "pandas": 0.7},
}


def test_with_jev_skills_select_asks_the_model_for_no_skills(wide_master, monkeypatch):
    system, user, _sel = _select_prompts(monkeypatch, skill_pick=_SKILL_PICK)
    assert "technical skills" not in system
    assert "SKILL POOLS" not in user
    assert '"skills":' not in user and '"skill_focus":' not in user
    # the rest of the request is unchanged: the methods ranking still rides along
    assert "METHODS POOL" in user and '"methods":' in user
    assert "concepts/methodologies" in system


def test_select_takes_the_skills_and_the_focus_from_jev(wide_master, monkeypatch):
    answer = {"experience": [], "projects": [], "leadership": [],
              "skill_focus": "ml_research", "skills": {"Languages": "Go"}, "rationale": ""}
    _system, _user, sel = _select_prompts(monkeypatch, answer=answer, skill_pick=_SKILL_PICK)
    assert sel["skills"] == {"Languages": "SQL, Python, Go", "Frameworks": "Flask",
                             "Developer Tools": "Docker, Git", "Libraries": "pandas"}
    assert sel["skill_focus"] == "data_analytics"


def test_compress_skills_fills_each_line_by_probability(wide_master, monkeypatch):
    def no_call(*a, **k):
        raise AssertionError("the skills fallback call ran with Jev's lines in hand")

    monkeypatch.setattr(skills, "call", no_call)
    _system, _user, sel = _select_prompts(monkeypatch, skill_pick=_SKILL_PICK)
    lines = {ln["label"]: ln["items"] for ln in compose.compress_skills(_JD, "Analyst", sel)}
    assert lines == {"Languages": "SQL, Python, Go", "Frameworks": "Flask",
                     "Developer Tools": "Docker, Git", "Libraries": "pandas"}


def test_a_line_keeps_its_count_and_width(wide_master, monkeypatch):
    """Code decides the length: the most probable skills fill the line's count
    (layout.skill_targets) and then the printed width."""
    monkeypatch.setattr(skills, "call", lambda *a, **k: {})
    monkeypatch.setenv("RESUME_TAILOR_SKILL_TARGETS", "Languages=2")
    _system, _user, sel = _select_prompts(monkeypatch, skill_pick=_SKILL_PICK)
    lines = {ln["label"]: ln["items"] for ln in compose.compress_skills(_JD, "Analyst", sel)}
    assert lines["Languages"] == "SQL, Python"
    monkeypatch.delenv("RESUME_TAILOR_SKILL_TARGETS")
    monkeypatch.setattr(measure, "SKILL_LINE_CAPACITY",
                        measure.skill_line_width("Languages", "SQL"))
    lines = {ln["label"]: ln["items"] for ln in compose.compress_skills(_JD, "Analyst", sel)}
    assert lines["Languages"] == "SQL"


# ── TL-3: the lead bullet ────────────────────────────────────────────────────
def _p1_sel():
    """P1 as select left it: p1_2 first, though p1_1 is the earlier authored atom."""
    return {"experience": [], "leadership": [],
            "projects": [{"name": "P1", "groups": [["p1_2"], ["p1_1"]]}]}


def _no_llm(*a, **k):
    raise AssertionError("the ordering call ran with Jev's answer in hand")


@pytest.mark.parametrize("confidence", [0.9, 0.5])
def test_a_sure_jev_pick_leads_and_the_ordering_call_is_skipped(wide_master, monkeypatch,
                                                                confidence):
    monkeypatch.setattr(compose, "call", _no_llm)
    sel = _p1_sel()
    compose.lead_with_overview(_JD, "Analyst", sel, judge=_Pick("1", confidence))
    assert sel["projects"][0]["groups"] == [["p1_2"], ["p1_1"]]


def test_an_unsure_jev_pick_keeps_file_order(wide_master, monkeypatch):
    monkeypatch.setattr(compose, "call", _no_llm)
    sel = _p1_sel()
    compose.lead_with_overview(_JD, "Analyst", sel, judge=_Pick("1", 0.4))
    assert sel["projects"][0]["groups"] == [["p1_1"], ["p1_2"]]


def test_a_failing_judge_leaves_the_ordering_call_as_it_was(wide_master, monkeypatch):
    prompts = []

    def fake_call(system, user, tier, **kw):
        prompts.append((system, user, tier, kw))
        return {"projects": [{"project": "P1", "lead": 1}]}

    monkeypatch.setattr(compose, "call", fake_call)
    off, failed = _p1_sel(), _p1_sel()
    compose.lead_with_overview(_JD, "Analyst", off)
    compose.lead_with_overview(_JD, "Analyst", failed, judge=_Failing())
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    assert failed == off
    assert failed["projects"][0]["groups"] == [["p1_2"], ["p1_1"]]


def test_with_no_project_to_order_the_lead_line_says_so(wide_master, monkeypatch):
    monkeypatch.setattr(compose, "call", _no_llm)
    rec = _Recording(jev.FakeJev())
    jev_assist.reset_usage()
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "P1", "groups": [["p1_1"]]}]}
    compose.lead_with_overview(_JD, "Analyst", sel, judge=rec)
    assert rec.requests == []
    assert sel["projects"][0]["groups"] == [["p1_1"]]
    assert jev_assist.usage_line(jev_assist.STEP_LEAD) == (
        "jev lead: 0 requests, 0 tokens (estimated), $0.000000; nothing to ask")
