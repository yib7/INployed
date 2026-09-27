"""The tailor's Jev steps (TL-1 to TL-6) as `run.tailor()` meets them.

With Jev off the tailor must make exactly the LLM calls it made before cycle 19,
with byte-identical prompts. `test_jev_off_prompts_match_the_recording` pins that:
the prompts of the stages the Jev steps sit beside and the order and tier of every
call a golden run makes were recorded from the engine before the Jev wiring landed,
into `tests/fixtures/tailor_jev_off_prompts.json`. Part 4a recorded `select`, the
`lead_with_overview` ordering call and `compress_skills`' fallback call; part 4b
added the stages its checks gate (both `reverb` calls, every AI-writing sweep call
and a `reground` re-ask), recorded at its base before the engine changed, and part
4c added the rephrase call that best-of-N (TL-7) gates, recorded the same way.
A prompt change made on purpose re-records the file (set TAILOR_JEV_OFF_PROMPTS_RECORD=1
for one run) and shows the diff in review; a change nobody meant fails here.

The rest covers each Jev step where its answer lands (the shortlist and the skills
in `select`, the lead in `lead_with_overview`, the new verb for a repeated opener in
`dedupe_leading_verbs`) and whole runs with Jev on, with Jev
down from the start and with an outage mid-run. The faithfulness check (TL-4) has its
own module, `test_tailor_faithfulness.py`, and so does the sweep gate (TL-5),
`test_sweep_gate.py`; the whole runs here count their requests.

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
                           output, render, selection, skills, sweep, verify)
from resume_tailor import run as rt_run  # noqa: E402
from resume_tailor.compile import CompileResult  # noqa: E402

import test_tailor_golden as golden  # noqa: E402 - sibling test module, no pkg import

# The golden module's fixtures, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

PROMPTS = REPO / "tests" / "fixtures" / "tailor_jev_off_prompts.json"
RECORD_ENV = "TAILOR_JEV_OFF_PROMPTS_RECORD"
# The stages whose prompts are pinned word for word: the ones a Jev step replaces,
# trims or gates when Jev is on. A stage the golden run calls more than once is
# recorded once per call, in call order: its first call under the stage's name and
# each later one as "<stage> #<n>", so the recording 4a made stays as it was.
PINNED_STAGES = ("select", "lead_with_overview", "rephrase", "reverb", "aiwriting_sweep")
# The tailor's three Jev options (TL-7 to TL-9). Each needs a judge, so with Jev off
# the recording holds whichever way they are set.
JEV_OPTION_ENVS = ("RESUME_TAILOR_BEST_OF_N", "RESUME_TAILOR_COVER_LETTER_JEV_CHECK",
                   "RESUME_TAILOR_ATS_MEANING")

# compress_skills' fallback answer: any pool-backed lines will do, the prompt is
# what is recorded.
_FALLBACK_SKILLS = {"Languages": "Python, SQL", "Frameworks": "FastAPI",
                    "Developer Tools": "Git", "Libraries": "pandas"}

# The golden run drops no bullet, so it never reaches the reground re-ask; one
# direct call over the run's own selection records that prompt. Two groups: one
# with a length target and one fused by the fill pass.
_REGROUND_DROPPED = {"gx_dbt": ["kubernetes"], "th_overview+th_api": ["terraform", "graphql"]}


def _recording(stages, calls):
    """The golden stub, recording every call's prompts, tier and options."""
    stub = golden._make_stub(stages)

    def _call(system, user, tier, **kw):
        out = stub(system, user, tier, **kw)
        calls.append({"stage": stages[-1], "system": system, "user": user,
                      "tier": tier, "kw": kw})
        return out
    return _call


def _seeing(stages, systems):
    """The golden stub, keeping every system prompt BEFORE the stub answers. The stub
    raises on the skills fallback call, and `compress_skills` swallows that error, so
    only a prompt kept first can show that the call was made."""
    stub = golden._make_stub(stages)

    def _call(system, user, tier, **kw):
        systems.append(system)
        return stub(system, user, tier, **kw)
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


def _prompt(call):
    return {"system": _lines(call["system"]), "user": _lines(call["user"])}


def _record_jev_off(monkeypatch, tmp_path):
    """Everything the recording pins, from one golden run plus one reground call and
    one skills fallback call."""
    stages: list = []
    calls: list = []
    golden._install_stub(monkeypatch, _recording(stages, calls))
    captured = _run_tailor(monkeypatch, tmp_path)

    reground: dict = {}

    def _reground_call(system, user, tier, **kw):
        reground.update(system=system, user=user, tier=tier, kw=kw)
        return {"bullets": []}

    monkeypatch.setattr(compose, "call", _reground_call)
    compose.reground(golden._JD, golden._JOB["job_title"], captured["sel"],
                     {gk: list(tokens) for gk, tokens in _REGROUND_DROPPED.items()})

    fallback: dict = {}

    def _fallback_call(system, user, tier, **kw):
        fallback.update(system=system, user=user, tier=tier, kw=kw)
        return dict(_FALLBACK_SKILLS)

    monkeypatch.setattr(skills, "call", _fallback_call)
    compose.compress_skills(golden._JD, golden._JOB["job_title"],
                            {"skills": {}, "skill_focus": "data_analytics"})

    prompts: dict = {}
    seen: dict = {}
    for c in calls:
        if c["stage"] in PINNED_STAGES:
            n = seen[c["stage"]] = seen.get(c["stage"], 0) + 1
            prompts[c["stage"] if n == 1 else f"{c['stage']} #{n}"] = _prompt(c)
    prompts["reground"] = _prompt(reground)
    prompts["skills_fallback"] = _prompt(fallback)
    return {
        "calls": [{"stage": c["stage"], "tier": c["tier"], "kw": c["kw"]} for c in calls],
        "prompts": prompts,
        "skills_fallback_call": {"tier": fallback["tier"], "kw": fallback["kw"]},
        "reground_call": {"tier": reground["tier"], "kw": reground["kw"]},
    }


def _jev_off(monkeypatch):
    """Jev off for the tailor whatever the environment holds: the switch hands out
    no client, as it does with no TypeSafe key (the suite's default)."""
    monkeypatch.setattr(jev_switch, "client", lambda area: None)


@pytest.mark.parametrize("options", ["0", "1"], ids=["options off", "options on"])
def test_jev_off_prompts_match_the_recording(pinned_engine, stub_template_head,
                                             tmp_path, monkeypatch, options):
    """Jev off makes exactly the recorded calls, and the stages beside the Jev
    steps send the recorded prompts, the rephrase among them. The recording is made
    with the three Jev options off, and they change nothing while Jev is off."""
    _jev_off(monkeypatch)
    for env in JEV_OPTION_ENVS:
        monkeypatch.setenv(env, options)
    got = _record_jev_off(monkeypatch, tmp_path)
    if os.environ.get(RECORD_ENV) == "1" and options == "0":
        PROMPTS.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
        pytest.skip(f"recorded {PROMPTS.name}; run again without {RECORD_ENV}")
    want = json.loads(PROMPTS.read_text(encoding="utf-8"))
    assert [c["stage"] for c in got["calls"]] == [c["stage"] for c in want["calls"]]
    assert got["calls"] == want["calls"]
    assert list(got["prompts"]) == list(want["prompts"])
    for stage in want["prompts"]:
        assert got["prompts"][stage]["system"] == want["prompts"][stage]["system"], stage
        assert got["prompts"][stage]["user"] == want["prompts"][stage]["user"], stage
    assert got["skills_fallback_call"] == want["skills_fallback_call"]
    assert got["reground_call"] == want["reground_call"]
    assert got == want


def test_the_recording_covers_every_gated_stage():
    """Every stage 4a, 4b and 4c gate has its prompt in the recording: select, the
    lead call and the rephrase once, reverb twice, the sweep once per item, and the
    reground re-ask."""
    want = json.loads(PROMPTS.read_text(encoding="utf-8"))
    assert list(want["prompts"]) == [
        "select", "lead_with_overview", "rephrase", "reverb", "reverb #2", "aiwriting_sweep",
        "aiwriting_sweep #2", "aiwriting_sweep #3", "aiwriting_sweep #4", "reground",
        "skills_fallback"]
    assert "REJECTED BULLETS" in "\n".join(want["prompts"]["reground"]["user"])


# ── fake judges ──────────────────────────────────────────────────────────────
def _no_sleep(_seconds):
    pass


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


class ServiceDown(Exception):
    status = 503


class _DownAfter:
    """Answers its first `answers` requests like FakeJev, then the service is down."""

    def __init__(self, answers=0):
        self.answers = answers
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        if self.calls <= self.answers:
            return jev.FakeJev().judge(state, questions)
        raise ServiceDown("service unavailable")


def _jev_on(monkeypatch, inner):
    """Jev on for the tailor, with `inner` behind the run's guard; returns the list
    of areas the client was asked for."""
    areas = []

    def client(area):
        areas.append(area)
        return jev.Guarded(inner, sleep=_no_sleep)

    monkeypatch.setattr(jev_switch, "client", client)
    return areas


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
    # compress_skills swallows any error its fallback call raises, so a raising guard
    # would pass unseen; the calls are counted instead.
    fallback_calls = []
    monkeypatch.setattr(skills, "call", lambda *a, **k: fallback_calls.append(a) or {})
    _system, _user, sel = _select_prompts(monkeypatch, skill_pick=_SKILL_PICK)
    lines = {ln["label"]: ln["items"] for ln in compose.compress_skills(_JD, "Analyst", sel)}
    assert fallback_calls == [], "the skills fallback call ran with Jev's lines in hand"
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


def _ordering_calls(monkeypatch):
    """Counts the ordering calls. `lead_with_overview` swallows any error its model
    pass raises, so a raising guard would pass unseen. Each call answers bullet 1,
    which differs from P1's file order, so a call that ran shows in the result too."""
    calls = []

    def fake_call(system, user, tier, **kw):
        calls.append(system)
        return {"projects": [{"project": "P1", "lead": 1}]}

    monkeypatch.setattr(compose, "call", fake_call)
    return calls


@pytest.mark.parametrize("confidence", [0.9, 0.5])
def test_a_sure_jev_pick_leads_and_the_ordering_call_is_skipped(wide_master, monkeypatch,
                                                                confidence):
    calls = _ordering_calls(monkeypatch)
    sel = _p1_sel()
    compose.lead_with_overview(_JD, "Analyst", sel, judge=_Pick("1", confidence))
    assert calls == [], "the ordering call ran with Jev's answer in hand"
    assert sel["projects"][0]["groups"] == [["p1_2"], ["p1_1"]]


def test_an_unsure_jev_pick_keeps_file_order(wide_master, monkeypatch):
    calls = _ordering_calls(monkeypatch)
    sel = _p1_sel()
    compose.lead_with_overview(_JD, "Analyst", sel, judge=_Pick("1", 0.4))
    assert calls == [], "the ordering call ran with Jev's answer in hand"
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
    calls = _ordering_calls(monkeypatch)
    rec = _Recording(jev.FakeJev())
    jev_assist.reset_usage()
    sel = {"experience": [], "leadership": [],
           "projects": [{"name": "P1", "groups": [["p1_1"]]}]}
    compose.lead_with_overview(_JD, "Analyst", sel, judge=rec)
    assert rec.requests == [] and calls == []
    assert sel["projects"][0]["groups"] == [["p1_1"]]
    assert jev_assist.usage_line(jev_assist.STEP_LEAD) == (
        "jev lead: 0 requests, 0 tokens (estimated), $0.000000; nothing to ask")


# ── TL-6: the verb dedupe ────────────────────────────────────────────────────
_PALETTE = {"Build": ["Built", "Designed", "Engineered"],
            "Analyze": ["Analyzed", "Modeled", "Quantified"],
            "Lead": ["Led", "Coordinated"]}
_REVERBED = "Charted weekly demand for the ops team."


def _dedupe_bullets():
    """b repeats a's "Modeled"; c and d open with verbs no earlier bullet uses."""
    return {"a": "Modeled churn for the retention team.",
            "b": "Modeled weekly demand for the ops team.",
            "c": "Built the ingest service in Go.",
            "d": "Led a study group of 6."}


def _reverb_calls(monkeypatch):
    """Counts the `reverb` re-rolls, each answering `_REVERBED`, and pins the palette.
    `dedupe_leading_verbs` swallows any error `reverb` raises, so a raising guard
    would pass unseen."""
    calls = []

    def fake_reverb(jd, ids, bad_text, used):
        calls.append((list(ids), bad_text, sorted(used)))
        return _REVERBED

    monkeypatch.setattr(compose, "reverb", fake_reverb)
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in _PALETTE.items()})
    return calls


def _dedupe(bullets, judge, reserved=frozenset({"quantified"})):
    return compose.dedupe_leading_verbs(bullets, {gk: [gk] for gk in bullets}, _JD,
                                        reserved=reserved, judge=judge)


class _Staged:
    """Answers TL-6's category question with `category` and its verb question with
    `verb`, each (choice, confidence)."""

    def __init__(self, category, verb):
        self.category = category
        self.verb = verb

    def judge(self, state, questions):
        out = {}
        for qid, q in questions.items():
            choice, conf = self.category if qid == "category" else self.verb
            out[qid] = jev.Answer(kind="choice", choice=choice,
                                  probabilities={n: float(n == choice) for n in q["criteria"]},
                                  confidence=conf)
        return out


@pytest.mark.parametrize("confidence", [0.9, 0.5])
def test_a_sure_verb_pick_swaps_the_first_word_and_skips_reverb(monkeypatch, confidence):
    calls = _reverb_calls(monkeypatch)
    rec = _Recording(_Staged(("Analyze", 0.9), ("Analyzed", confidence)))
    got = _dedupe(_dedupe_bullets(), rec)
    assert calls == [], "reverb ran with Jev's answer in hand"
    assert got["b"] == "Analyzed weekly demand for the ops team."
    assert [got[k] for k in "acd"] == [_dedupe_bullets()[k] for k in "acd"]
    (state, first), (state2, second) = rec.requests
    assert state == state2 == {"bullet": "Modeled weekly demand for the ops team."}
    assert list(first["category"]["criteria"]) == ["Build", "Analyze", "Lead"]
    # The picked category's unused verbs. Out: the verbs earlier bullets and the
    # verbatim blocks open with ("modeled", "quantified") and the ones later
    # bullets open with ("built", "led").
    assert second["verb"]["criteria"] == {"Analyzed": "Analyze"}


def test_an_unsure_verb_pick_keeps_the_reverb_call(monkeypatch):
    calls = _reverb_calls(monkeypatch)
    got = _dedupe(_dedupe_bullets(), _Staged(("Analyze", 0.9), ("Analyzed", 0.4)))
    assert calls == [(["b"], "Modeled weekly demand for the ops team.",
                      ["modeled", "quantified"])]
    assert got["b"] == _REVERBED


def test_a_failing_judge_leaves_the_reverb_call_as_it_was(monkeypatch):
    calls = _reverb_calls(monkeypatch)
    off = _dedupe(_dedupe_bullets(), None)
    failed = _dedupe(_dedupe_bullets(), _Failing())
    assert len(calls) == 2 and calls[0] == calls[1]
    assert failed == off and off["b"] == _REVERBED


def test_each_repeated_opener_is_its_own_pair_of_choices(monkeypatch):
    """A verb picked for one bullet is used from then on. The next repeat of the
    same opener finds its category used up, so it asks nothing more and keeps the
    reverb call."""
    calls = _reverb_calls(monkeypatch)
    bullets = _dedupe_bullets()
    bullets["e"] = "Modeled the club budget for the spring term."
    rec = _Recording(_Staged(("Analyze", 0.9), ("Analyzed", 0.9)))
    got = _dedupe(dict(bullets), rec)
    assert [(s["bullet"], list(q)) for s, q in rec.requests] == [
        (bullets["b"], ["category"]), (bullets["b"], ["verb"]), (bullets["e"], ["category"])]
    assert got["b"] == "Analyzed weekly demand for the ops team."
    assert calls == [(["e"], bullets["e"], ["analyzed", "built", "led", "modeled", "quantified"])]
    assert got["e"] == _REVERBED


def test_the_real_palette_sends_two_choices_jev_takes(monkeypatch):
    """VL-3: over the repo's active_words.md (368 verbs), TL-6 asked one choice over
    every unused verb, past the 255 options Jev takes, and got a 400. It now asks the
    category, then a verb in it: for every category, both requests fit, and the
    second holds exactly that category's unused verbs."""
    monkeypatch.setattr(compose, "reverb", lambda *a, **k: _REVERBED)
    monkeypatch.setattr(config, "ACTIVE_WORDS_MD", REPO / "resume_tailor_files" / "active_words.md")
    assets.active_verbs.cache_clear()
    try:
        palette = assets.active_verbs()
    finally:
        assets.active_verbs.cache_clear()
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in palette.items()})
    assert len({v.lower() for vs in palette.values() for v in vs}) > jev.CHOICE_OPTIONS_MAX, \
        "the palette no longer outgrows one choice"
    verb = next(iter(palette.values()))[0]
    bullets = {"a": f"{verb} churn for the retention team.",
               "b": f"{verb} weekly demand for the ops team."}
    for cat in palette:
        rec = _Recording(_Staged((cat, 0.9), ("not a verb", 0.9)))
        _dedupe(dict(bullets), rec, reserved=frozenset())
        (s1, first), (s2, second) = rec.requests
        assert list(first["category"]["criteria"]) == list(palette)
        assert jev.request_fits(s1, first) and jev.request_fits(s2, second)
        want, seen = [], {verb.lower()}
        for v in palette[cat]:
            if v.lower() not in seen:
                seen.add(v.lower())
                want.append(v)
        assert list(second["verb"]["criteria"]) == want, cat
        assert set(second["verb"]["criteria"].values()) == {cat}
        assert 0 < len(want) <= jev.CHOICE_OPTIONS_MAX


def test_with_no_repeated_opener_the_verb_line_says_so(monkeypatch):
    calls = _reverb_calls(monkeypatch)
    rec = _Recording(jev.FakeJev())
    jev_assist.reset_usage()
    bullets = {k: v for k, v in _dedupe_bullets().items() if k != "b"}
    assert _dedupe(dict(bullets), rec) == bullets
    assert rec.requests == [] and calls == []
    assert jev_assist.usage_line(jev_assist.STEP_VERB) == (
        "jev verb: 0 requests, 0 tokens (estimated), $0.000000; nothing to ask")


# ── whole runs ───────────────────────────────────────────────────────────────
# FakeJev rates every golden skill and atom alike (0.1), so each skills line is its
# pool in the user's order and the shortlist is the whole catalog in file order.
_JEV_SKILL_LINES = [
    {"label": "Languages", "items": "Python, SQL, R, Java"},
    {"label": "Frameworks", "items": "FastAPI, Flask, Django"},
    {"label": "Developer Tools", "items": "Git, Docker, Postgres, Redis, dbt"},
    {"label": "Libraries", "items": "pandas, NumPy, scikit-learn"},
    {"label": "Methods", "items": "ETL, Experimentation, Data Modeling, Feature Engineering"},
]


def _report(tmp_path):
    return (tmp_path / "out" / rt_run.REPORT_NAME).read_text(encoding="utf-8")


def test_a_golden_run_with_jev_on_makes_fewer_llm_calls(pinned_engine, stub_template_head,
                                                        tmp_path, monkeypatch):
    """Jev answers the lead, so the ordering call goes; the bullets, the grounding
    gate and the page are the golden's. The skills lines come from Jev and the pools,
    with no fallback call: every system prompt is kept before the stub answers, and
    none is the ordering call's or the fallback call's.

    Jev also picks the new verb for both Trailhead bullets that repeat the verbatim
    block's "Built" (TL-6), so both `reverb` calls go. FakeJev finds no option's words
    in either bullet and takes the first option, the repeated verb's category first:
    "Designed", then "Engineered", the verbs the golden's two dedupe arms reach.

    The sweep gate (TL-5) asks once per sweep item (4 requests). FakeJev reads no tell
    in any golden bullet and no detector fires on them, so all four sweep calls go too:
    the golden stub only echoed them.

    The faithfulness check (TL-4) passes every golden bullet. It asks once per entry
    after the rephrase (4 requests) and once per entry a later pass rewrote: Trailhead
    after the verb dedupe and after the fill, Globex Analytics after the style gate
    (rc_workshop's change there is the em-dash strip alone). The sweep makes no call,
    so it changes nothing to ask about."""
    systems: list = []
    golden._install_stub(monkeypatch, _seeing(pinned_engine, systems))
    rec = _Recording(jev.FakeJev())
    areas = _jev_on(monkeypatch, rec)
    gate = []
    real_gate = verify.enforce_grounded

    def _recording_gate(sel, bullets, *, fallback=None, log=None):
        gate.append(fallback is None)
        return real_gate(sel, bullets, fallback=fallback, log=log)

    monkeypatch.setattr(verify, "enforce_grounded", _recording_gate)
    captured = _run_tailor(monkeypatch, tmp_path)
    assert len(systems) == len(pinned_engine)
    assert not any("PURE ORDERING" in s for s in systems), "the ordering call ran"
    assert not any("EXACTLY FOUR fixed lines" in s for s in systems), "the fallback ran"
    assert not any("OPENS WITH A DIFFERENT action verb" in s for s in systems), "reverb ran"
    assert not any("You clean AI-writing tells" in s for s in systems), "the sweep ran"
    assert pinned_engine == [s for s in golden._GOLDEN_STAGES
                             if s not in ("lead_with_overview", "reverb", "aiwriting_sweep")]
    assert len(pinned_engine) == len(golden._GOLDEN_STAGES) - 7
    assert captured["bullets"] == golden._GOLDEN_BULLETS
    assert gate == [True, False, False, False, False]
    assert captured["skill_lines"] == _JEV_SKILL_LINES
    assert areas == ["tailor"], "one judge per run, handed to every step"
    # skills, shortlist, lead; two per repeated opener (TL-6); sweep; faithfulness
    assert len(rec.requests) == 3 + 2 * 2 + 4 + 7
    verbs = [state["bullet"] for state, questions in rec.requests if "verb" in questions]
    assert verbs == [
        "Built Trailhead, a hiking route planner that ranks trails for a given weather window.",
        "Built a gradient boosting model on 8,400 logged hikes to predict trail difficulty."]
    reads = [state["entry"] for state, questions in rec.requests if "contrast_0" in questions]
    assert reads == ["Globex Analytics", "Trailhead", "Ledgerly", "Robotics Club"]
    faith = [state["entry"] for state, questions in rec.requests
             if "supported_0" in questions]
    assert faith == ["Globex Analytics", "Trailhead", "Ledgerly", "Robotics Club",
                     "Trailhead", "Trailhead", "Globex Analytics"]
    report = _report(tmp_path)
    assert "warnings (0)" in report and "jev (6)" in report
    for step in ("skills", "shortlist", "lead"):
        assert f"  jev {step}: 1 request, " in report
    assert "  jev verb: 4 requests, " in report
    assert "  jev sweep gate: 4 requests, " in report
    assert "  jev faithfulness: 7 requests, " in report
    assert "fell back" not in report
    assert ("[ai writing sweep] swept 0 item(s) in 0 call(s) and rewrote 0 bullet(s)"
            in report)
    assert ("[ai writing sweep] Jev's sweep gate skipped the call for 4 item(s) with no "
            "tell and no detector finding: Globex Analytics, Trailhead, Ledgerly, "
            "Robotics Club") in report
    assert "] Jev read " not in report, "no bullet is flagged, so no flag note is written"
    assert "] faithfulness:" not in report, "no bullet is flagged, so no note is written"


class _ReadsHype:
    """FakeJev, except that it reads hype words in the bullet holding `marker`."""

    def __init__(self, marker):
        self.marker = marker
        self.fake = jev.FakeJev()

    def judge(self, state, questions):
        out = self.fake.judge(state, questions)
        for qid in questions:
            if qid.startswith("hype_") and self.marker in state["bullets"][int(qid[5:])]:
                out[qid] = jev.Answer(kind="noul", noul=0.9)
        return out


def test_a_bullet_jev_flags_sends_its_item_to_the_sweep(pinned_engine, stub_template_head,
                                                        tmp_path, monkeypatch):
    """TL-5 in a whole run: Jev reads hype words in rc_lead, so Robotics Club alone
    makes its sweep call, with the flag in its payload and the flag rule in its system
    prompt. The stub echoes, so the page is the golden's."""
    stages: list = []
    calls: list = []
    golden._install_stub(monkeypatch, _recording(stages, calls))
    _jev_on(monkeypatch, _ReadsHype("regional finals"))
    captured = _run_tailor(monkeypatch, tmp_path)
    sweeps = [c for c in calls if c["stage"] == "aiwriting_sweep"]
    assert len(sweeps) == 1
    assert sweeps[0]["system"] == sweep._SWEEP_SYSTEM + sweep._SWEEP_FLAG_RULE
    assert sweep._SWEEP_FLAG_CLOSING in sweeps[0]["user"]
    body = golden._item_body(sweeps[0]["user"])
    assert body["item"] == "Robotics Club"
    assert {b["gkey"]: b["judge_flags"] for b in body["bullets"]} == {
        "rc_lead": ["hype words"], "rc_workshop": []}
    assert captured["bullets"] == golden._GOLDEN_BULLETS
    report = _report(tmp_path)
    assert ("[ai writing sweep] Jev read hype words in bullet 'rc_lead' in 'Robotics "
            "Club', so that item was sent to the sweep") in report
    assert ("[ai writing sweep] Jev's sweep gate skipped the call for 3 item(s) with no "
            "tell and no detector finding: Globex Analytics, Trailhead, Ledgerly") in report
    assert "[ai writing sweep] swept 1 item(s) in 1 call(s) and rewrote 0 bullet(s)" in report


def test_jev_down_from_the_start_makes_exactly_the_jev_off_calls(
        pinned_engine, stub_template_head, tmp_path, monkeypatch):
    """The first step spends the retries and opens the breaker; the others never
    reach the service. Every call and pinned prompt is the Jev-off recording's, and
    the faithfulness check leaves the grounding gate to stand alone."""
    down = _DownAfter(answers=0)
    _jev_on(monkeypatch, down)
    got = _record_jev_off(monkeypatch, tmp_path)
    assert got == json.loads(PROMPTS.read_text(encoding="utf-8"))
    assert down.calls == len(jev.RETRY_DELAYS_S) + 1
    report = _report(tmp_path)
    for step in ("skills", "shortlist", "lead", "verb", "sweep gate"):
        assert (f"  jev {step}: 0 requests, 0 tokens (estimated), $0.000000; fell back to "
                "the LLM path (JudgeOutage ServiceDown 503)") in report
    assert report.count("fell back to the LLM path (JudgeOutage ServiceDown 503)") == 5
    assert report.count("fell back to the deterministic gate alone "
                        "(JudgeOutage ServiceDown 503)") == 1


def test_an_outage_mid_run_moves_the_rest_of_the_run_to_the_llm_path(
        pinned_engine, stub_template_head, tmp_path, monkeypatch):
    """Jev answers the skills and then goes down: the shortlist and the lead fall
    back to today's path (the whole catalog, the ordering call), and the run ends
    with the golden's bullets."""
    stages: list = []
    calls: list = []
    golden._install_stub(monkeypatch, _recording(stages, calls))
    _jev_on(monkeypatch, _DownAfter(answers=1))
    captured = _run_tailor(monkeypatch, tmp_path)
    assert stages == golden._GOLDEN_STAGES
    assert captured["bullets"] == golden._GOLDEN_BULLETS
    assert captured["skill_lines"] == _JEV_SKILL_LINES
    select_user = calls[0]["user"]
    assert "SKILL POOLS" not in select_user
    assert _catalog_ids(select_user) == list(assets.atoms_by_id())
    report = _report(tmp_path)
    assert "  jev skills: 1 request, " in report
    for step in ("shortlist", "lead", "verb", "sweep gate"):
        assert (f"  jev {step}: 0 requests, 0 tokens (estimated), $0.000000; fell back to "
                "the LLM path (JudgeOutage ServiceDown 503)") in report
    assert ("  jev faithfulness: 0 requests, 0 tokens (estimated), $0.000000; fell back "
            "to the deterministic gate alone (JudgeOutage ServiceDown 503)") in report


def test_jev_off_leaves_the_report_and_the_status_log_as_they_were(
        pinned_engine, stub_template_head, tmp_path, monkeypatch):
    _jev_off(monkeypatch)
    statuses: list = []
    _run_tailor(monkeypatch, tmp_path, on_status=statuses.append)
    report = _report(tmp_path).splitlines()
    assert not any(line.startswith(("jev (", "  jev ")) for line in report)
    assert not any(s.startswith("jev ") for s in statuses)


def test_jev_off_calls_the_gated_stages_as_they_were_called(
        pinned_engine, stub_template_head, tmp_path, monkeypatch):
    """With Jev off the verb dedupe and the sweep are called with the signatures they
    had before cycle 19 (no `judge=`), so a caller or a test double written against
    those signatures keeps working."""
    _jev_off(monkeypatch)
    real_dedupe, real_sweep = compose.dedupe_leading_verbs, sweep.sweep_items
    seen = []

    def dedupe(bullets, gm, jd, *, reserved=frozenset()):
        seen.append("dedupe")
        return real_dedupe(bullets, gm, jd, reserved=reserved)

    def sweep_items(jd, job_title, sel, bullets):
        seen.append("sweep")
        return real_sweep(jd, job_title, sel, bullets)

    monkeypatch.setattr(compose, "dedupe_leading_verbs", dedupe)
    monkeypatch.setattr(sweep, "sweep_items", sweep_items)
    captured = _run_tailor(monkeypatch, tmp_path)
    assert seen == ["dedupe", "sweep"]
    assert captured["bullets"] == golden._GOLDEN_BULLETS


def test_the_usage_lines_reach_the_status_log(pinned_engine, stub_template_head,
                                              tmp_path, monkeypatch):
    statuses: list = []
    _jev_on(monkeypatch, jev.FakeJev())
    _run_tailor(monkeypatch, tmp_path, on_status=statuses.append)
    jev_lines = [s for s in statuses if s.startswith("jev ")]
    assert jev_lines == [jev_assist.usage_line(step) for step in (
        jev_assist.STEP_SKILLS, jev_assist.STEP_SHORTLIST, jev_assist.STEP_LEAD,
        jev_assist.STEP_VERB, jev_assist.STEP_SWEEP_GATE, jev_assist.STEP_FAITHFULNESS)]
    assert [s.split(":")[0] for s in jev_lines] == ["jev skills", "jev shortlist", "jev lead",
                                                   "jev verb", "jev sweep gate",
                                                   "jev faithfulness"]
    report = _report(tmp_path)
    assert all(f"  {line}" in report for line in jev_lines)


def test_each_run_counts_its_own_jev_requests(pinned_engine, stub_template_head,
                                              tmp_path, monkeypatch):
    """A dashboard worker thread tailors one job after another: each run's usage
    lines count that run's requests alone."""
    _jev_on(monkeypatch, jev.FakeJev())
    _run_tailor(monkeypatch, tmp_path)
    _run_tailor(monkeypatch, tmp_path)
    report = _report(tmp_path)
    for step in ("skills", "shortlist", "lead"):
        assert f"  jev {step}: 1 request, " in report
    assert "  jev verb: 4 requests, " in report
    assert "  jev sweep gate: 4 requests, " in report
    assert "  jev faithfulness: 7 requests, " in report
