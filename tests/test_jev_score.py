"""SC-1 to SC-3 and JS-4 (cycle 19): the Jev scorer's switch, its questions,
the requirement-line extractor and the composition code.

Hermetic: the dashboard config is a file in tmp_path (monkeypatched onto
`jev_score.CONFIG_PATH`), the environment is a dict, the SDK probe is patched,
and every judge is scripted. No key, no network.
"""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import jev
import jev_score
import jev_switch

REPO = Path(__file__).resolve().parents[1]
KEY = {"TYPESAFE_API_KEY": "not-a-real-key"}


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Write the dashboard config the scorer reads; `cfg_file(None)` removes it."""
    path = tmp_path / "local" / "config.json"
    path.parent.mkdir()
    monkeypatch.setattr(jev_score, "CONFIG_PATH", path)

    def write(data) -> Path:
        if data is None:
            path.unlink(missing_ok=True)
        elif isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data), encoding="utf-8")
        return path
    return write


@pytest.fixture
def sdk(monkeypatch):
    """The SDK probe both switches use, patched: `sdk(False)` reads it as missing."""
    state = {"found": True}
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "typesafe_sdk":
            return object() if state["found"] else None
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    def set_found(found: bool) -> None:
        state["found"] = found
    return set_found


# --- JS-4: the switch order ---------------------------------------------------------

def test_env_on_wins_over_a_config_that_is_off(cfg_file, sdk, capsys):
    cfg_file({"jev_enabled": False})
    on, why = jev_score.use_jev({"SCORE_USE_JEV": "1", **KEY})
    assert on is True
    assert "SCORE_USE_JEV" in why
    assert capsys.readouterr().out == ""


def test_env_off_wins_over_a_config_that_is_on(cfg_file, sdk, capsys):
    cfg_file({"jev_enabled": True, "jev_scoring": True})
    for raw in ("0", "false", "no", "off", "later"):
        on, why = jev_score.use_jev({"SCORE_USE_JEV": raw, **KEY})
        assert on is False, raw
        assert "SCORE_USE_JEV" in why, raw
    assert capsys.readouterr().out == ""


def test_a_blank_env_switch_reads_as_unset(cfg_file, sdk):
    cfg_file({"jev_enabled": False})
    on, why = jev_score.use_jev({"SCORE_USE_JEV": "   ", **KEY})
    assert on is False
    assert why == "Jev is switched off in Settings"


def test_the_config_decides_when_the_env_is_silent(cfg_file, sdk):
    cfg_file({})
    assert jev_score.use_jev(KEY)[0] is True
    cfg_file({"jev_enabled": True, "jev_scoring": True})
    assert jev_score.use_jev(KEY) == (True, "Settings")
    cfg_file({"jev_enabled": False})
    assert jev_score.use_jev(KEY) == (False, "Jev is switched off in Settings")
    cfg_file({"jev_scoring": False})
    assert jev_score.use_jev(KEY) == (False, "Jev is switched off for scoring in Settings")


def test_no_config_and_no_env_stays_off_without_a_warning(cfg_file, sdk, capsys):
    """The VM case: no dashboard config, no SCORE_USE_JEV. Off by construction,
    even with a key and the SDK present, and silent about it."""
    cfg_file(None)
    on, why = jev_score.use_jev(KEY)
    assert on is False
    assert why
    assert capsys.readouterr().out == ""


def test_a_flat_copy_has_no_config_path(tmp_path, sdk, capsys, monkeypatch):
    """The VM copies the pipeline scripts flat into ~/, where there is no
    local/config.json beside them: CONFIG_PATH is None there, so Jev is off."""
    shutil.copy(REPO / "pipeline" / "jev_score.py", tmp_path / "jev_score.py")
    spec = importlib.util.spec_from_file_location("jev_score_flat", tmp_path / "jev_score.py")
    flat = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "jev_score_flat", flat)    # a dataclass looks itself up
    spec.loader.exec_module(flat)
    assert flat.CONFIG_PATH is None
    assert flat.use_jev(KEY)[0] is False
    assert capsys.readouterr().out == ""


def test_a_missing_key_prints_one_warning_and_is_off(cfg_file, sdk, capsys):
    cfg_file({})
    for env in ({}, {"TYPESAFE_API_KEY": ""}, {"TYPESAFE_API_KEY": "  "}):
        assert jev_score.use_jev(env) == (False, "no TypeSafe API key")
        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "WARNING" in out and "LLM" in out


def test_a_missing_sdk_prints_one_warning_and_is_off(cfg_file, sdk, capsys):
    cfg_file({})
    sdk(False)
    assert jev_score.use_jev(KEY) == (False, "typesafe-sdk is not installed")
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "WARNING" in out
    assert "not-a-real-key" not in out


def test_a_missing_jev_module_prints_one_warning_and_is_off(cfg_file, sdk, capsys, monkeypatch):
    cfg_file({})

    def no_jev():
        raise ImportError("no module named jev")
    monkeypatch.setattr(jev_score, "_jev_module", no_jev)
    on, why = jev_score.use_jev(KEY)
    assert on is False and "jev" in why
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "WARNING" in out


def test_the_env_switch_still_needs_the_key(cfg_file, sdk, capsys):
    cfg_file(None)
    assert jev_score.use_jev({"SCORE_USE_JEV": "yes"}) == (False, "no TypeSafe API key")
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.parametrize("cfg", [
    {}, {"jev_enabled": True}, {"jev_enabled": False}, {"jev_scoring": False},
    {"jev_enabled": True, "jev_scoring": False}, {"jev_enabled": False, "jev_scoring": True},
    {"jev_enabled": "false"}, {"jev_scoring": 0}, {"jev_enabled": None},
    "{not json", "[1, 2]",
])
@pytest.mark.parametrize("env", [{}, KEY])
@pytest.mark.parametrize("found", [True, False])
def test_use_jev_agrees_with_jev_switch_on_one_config_file(cfg_file, sdk, monkeypatch, capsys,
                                                           cfg, env, found):
    """The scorer's own switch (JS-4) and the dashboard's (`jev_switch`) read the
    same file the same way, so Settings and the scorer never disagree."""
    path = cfg_file(cfg)
    monkeypatch.setattr(jev_switch, "config_path", lambda: path)
    sdk(found)
    on, why = jev_score.use_jev(env)
    assert on is jev_switch.jev_on("scoring", env=env)
    assert why == (jev_switch.jev_why_off("scoring", env=env) or "Settings")


# --- SC-1: importing score_jobs stays light -------------------------------------------

def _copy_pipeline(dest: Path, names) -> Path:
    dest.mkdir(parents=True)
    for name in names:
        shutil.copy(REPO / "pipeline" / name, dest / name)
    return dest


def _run_python(code: str, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("TYPESAFE_API_KEY", "SCORE_USE_JEV")}
    env["INPLOYED_NO_DOTENV"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=90)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_importing_score_jobs_imports_neither_the_sdk_nor_jev(tmp_path):
    pipe = _copy_pipeline(tmp_path / "pipeline",
                          ("score_jobs.py", "jev_score.py", "keypool.py", "run_labels.py"))
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is not None)\n"
        "print(sorted(m for m in ('typesafe_sdk', 'jev') if m in sys.modules))\n",
        pipe)
    assert out.splitlines() == ["True", "[]"]


def test_score_jobs_runs_on_the_llm_path_without_jev_score_beside_it(tmp_path):
    """The VM copy may lack jev_score.py: the import guard reads that as Jev off."""
    flat = _copy_pipeline(tmp_path / "vm", ("score_jobs.py", "keypool.py", "run_labels.py"))
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is None)\n"
        "print(score_jobs.make_jev_judge() is None)\n",
        flat)
    lines = out.splitlines()
    assert lines[0] == "True"
    assert "not beside score_jobs.py" in out and "WARNING" not in out
    assert lines[-1] == "True"


def test_a_jev_score_that_fails_to_import_warns_once_and_keeps_the_llm_path(tmp_path):
    flat = _copy_pipeline(tmp_path / "vm", ("score_jobs.py", "keypool.py", "run_labels.py"))
    (flat / "jev_score.py").write_text("raise RuntimeError('broken on purpose')\n",
                                       encoding="utf-8")
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is None)\n"
        "print(score_jobs.make_jev_judge() is None)\n",
        flat)
    lines = out.splitlines()
    assert lines[0] == "True" and lines[-1] == "True"
    assert out.count("WARNING") == 1 and "RuntimeError" in out
    assert "broken on purpose" not in out


# --- scripted judges ------------------------------------------------------------------

class ScriptedJudge:
    """Answers every question from `table` (question id -> noul probability,
    choice name or raw score level); unlisted questions get a yes, the first
    option or the top level. Records every request."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def judge(self, state, questions):
        self.calls.append((state, questions))
        out = {}
        for qid, q in questions.items():
            want = self.table.get(qid)
            if q["type"] == "noul":
                out[qid] = jev.Answer(kind="noul", noul=0.9 if want is None else float(want))
            elif q["type"] == "choice":
                name = want or next(iter(q["criteria"]))
                out[qid] = jev.Answer(kind="choice", choice=name, confidence=1.0,
                                      probabilities={n: float(n == name) for n in q["criteria"]})
            else:
                top = len(q["criteria"]) - 1
                level = top if want is None else float(want)
                out[qid] = jev.Answer(kind="score", score=level, confidence=1.0,
                                      probabilities={str(i): float(i == round(level))
                                                     for i in range(top + 1)})
        return out


class FailingJudge:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        raise self.exc


NO_FACTS = {"min_years": None, "advanced_degree": False, "clearance": False}
RESUME = "Python, SQL and Tableau dashboards in a data science internship."
JOB_MD = ("## About the role\nYou will query data and build dashboards.\n\n"
          "## Requirements\n- Python and SQL\n- Tableau or Power BI dashboards\n"
          "- Statistics coursework\n")


def _reads(skills=0.86, domain="data_analytics_bi", label="entry_level", analytical=0.9):
    return {"skills_fit": skills, "domain": domain, "experience_label": label,
            "analytical_work": analytical}


# --- SC-2: stage 1 composition ------------------------------------------------------------

def test_the_spec_example_reason_is_composed_word_for_word():
    score, reason = jev_score.compose_stage1(NO_FACTS, _reads())
    assert score == 5
    assert reason == ("Skills fit strong (0.86); domain data analytics; "
                      "entry level (no years stated); no degree bar")


@pytest.mark.parametrize("facts,reads,score,fragment", [
    # in domain, no experience bar: skills_fit alone maps to 3-5
    (NO_FACTS, _reads(skills=1.0), 5, "Skills fit strong (1.00)"),
    (NO_FACTS, _reads(skills=0.80), 5, "strong"),
    (NO_FACTS, _reads(skills=0.75), 4, "Skills fit good (0.75)"),
    (NO_FACTS, _reads(skills=0.55), 4, "good"),
    (NO_FACTS, _reads(skills=0.50), 3, "Skills fit partial (0.50)"),
    (NO_FACTS, _reads(skills=0.10), 3, "Skills fit weak (0.10)"),
    (NO_FACTS, _reads(domain="data_science_ml"), 5, "domain data science or ML"),
    (NO_FACTS, _reads(domain="software_engineering"), 5, "domain software engineering"),
    (NO_FACTS, _reads(label="none_stated"), 5, "no experience level stated"),
    # code years win over the label, cap at 3 and lower with the requirement
    (dict(NO_FACTS, min_years=0), _reads(label="three_plus_years"), 5, "0-year floor"),
    (dict(NO_FACTS, min_years=1), _reads(skills=1.0), 3, "1+ years required"),
    (dict(NO_FACTS, min_years=2), _reads(), 3, "2+ years required"),
    (dict(NO_FACTS, min_years=3), _reads(), 2, "3+ years required"),
    (dict(NO_FACTS, min_years=4), _reads(), 2, "4+ years required"),
    (dict(NO_FACTS, min_years=5), _reads(), 1, "5+ years required"),
    (dict(NO_FACTS, min_years=2), _reads(skills=0.1), 3, "2+ years required"),
    # the label counts only when code found no years
    (NO_FACTS, _reads(label="one_to_two_years"), 3, "a floor of 1 to 2 years"),
    (NO_FACTS, _reads(label="three_plus_years"), 2, "3 or more years"),
    (NO_FACTS, _reads(label="senior_title"), 2, "senior title"),
    # off domain gives 1-2, whatever the analytical read says for hardware
    (NO_FACTS, _reads(domain="hardware_embedded", analytical=0.9, skills=0.9), 2,
     "domain hardware or embedded"),
    (NO_FACTS, _reads(domain="hardware_embedded", skills=0.3), 1, "domain hardware or embedded"),
    (NO_FACTS, _reads(domain="non_technical", analytical=0.1, skills=0.9), 2, "domain non-technical"),
    (NO_FACTS, _reads(domain="non_technical", analytical=0.1, skills=0.2), 1, "domain non-technical"),
    # analytical duties keep a business-flavoured role in domain (the rubric's analyst rule)
    (NO_FACTS, _reads(domain="non_technical", analytical=0.9), 5,
     "domain non-technical with data or engineering duties"),
    # other technical work is a partial domain match unless the duties are data or engineering
    (NO_FACTS, _reads(domain="other_technical", analytical=0.1, skills=0.95), 3,
     "domain other technical"),
    (NO_FACTS, _reads(domain="other_technical", analytical=0.9, skills=0.95), 5,
     "domain other technical with data or engineering duties"),
    # a hard advanced degree gives 1-2; a clearance requirement gives 1
    (dict(NO_FACTS, advanced_degree=True), _reads(skills=0.9), 2, "advanced degree required"),
    (dict(NO_FACTS, advanced_degree=True), _reads(skills=0.3), 1, "advanced degree required"),
    (dict(NO_FACTS, clearance=True), _reads(skills=1.0), 1, "clearance required"),
    # the lowest cap wins
    (dict(NO_FACTS, min_years=2), _reads(domain="hardware_embedded", skills=0.9), 2, "2+ years"),
    (dict(NO_FACTS, min_years=5), _reads(domain="other_technical", analytical=0.1), 1, "5+ years"),
])
def test_stage1_composition_table(facts, reads, score, fragment):
    got, reason = jev_score.compose_stage1(facts, reads)
    assert got == score, reason
    assert fragment in reason
    assert not reason.startswith("ERROR")


def test_stage1_reason_names_the_degree_bar_only_when_there_is_one():
    _s, reason = jev_score.compose_stage1(NO_FACTS, _reads())
    assert reason.endswith("no degree bar") and "clearance" not in reason
    _s, reason = jev_score.compose_stage1(dict(NO_FACTS, advanced_degree=True, clearance=True),
                                          _reads())
    assert "advanced degree required" in reason and "clearance required" in reason


def test_stage1_asks_the_four_questions_the_spec_names():
    qs = jev_score.stage1_questions()
    assert {qid: q["type"] for qid, q in qs.items()} == {
        "skills_fit": "score", "domain": "choice", "experience_label": "choice",
        "analytical_work": "noul"}
    levels = qs["skills_fit"]["criteria"]
    assert len(levels) == 5
    assert "almost none of the tools and skills the job lists" in levels[0]
    assert "nearly all of the core tools and skills" in levels[-1]
    assert list(qs["domain"]["criteria"]) == [
        "data_science_ml", "data_analytics_bi", "software_engineering", "other_technical",
        "hardware_embedded", "non_technical"]
    assert list(qs["experience_label"]["criteria"]) == [
        "none_stated", "entry_level", "one_to_two_years", "three_plus_years", "senior_title"]
    for q in qs.values():
        assert "`job`" in json.dumps(q["instructions"])


def test_stage1_sends_one_request_with_the_candidate_resume_and_job():
    judge = ScriptedJudge({"skills_fit": 3.44, "domain": "data_analytics_bi",
                           "experience_label": "entry_level"})
    got = jev_score.stage1(judge, {"md": JOB_MD, "facts": NO_FACTS}, RESUME)
    assert got == {"score": 5, "reason": "Skills fit strong (0.86); domain data analytics; "
                                         "entry level (no years stated); no degree bar"}
    assert len(judge.calls) == 1
    state, questions = judge.calls[0]
    assert set(state) == {"candidate", "resume", "job"}
    assert state["resume"] == RESUME and state["job"] == JOB_MD
    cand = json.dumps(state["candidate"]).lower()
    for words in ("new grad", "may 2026", "entry", "location", "work authorization"):
        assert words in cand, words
    assert set(questions) == set(jev_score.stage1_questions())


def test_stage1_takes_a_plain_job_text_as_having_no_code_facts():
    got = jev_score.stage1(ScriptedJudge({"skills_fit": 3.44}), JOB_MD, RESUME)
    assert got["score"] == 5


@pytest.mark.parametrize("table", [
    {"domain": "astrology"},                       # an option the question never offered
    {"skills_fit": float("nan")},
    {"analytical_work": 1.7},
])
def test_stage1_returns_none_on_a_read_it_cannot_use(table):
    assert jev_score.stage1(ScriptedJudge(table), JOB_MD, RESUME) is None


def test_stage1_returns_none_on_a_missing_answer():
    class Partial(ScriptedJudge):
        def judge(self, state, questions):
            out = super().judge(state, questions)
            out.pop("domain")
            return out
    assert jev_score.stage1(Partial(), JOB_MD, RESUME) is None


def test_stage1_returns_none_without_a_judge_or_a_job():
    assert jev_score.stage1(None, JOB_MD, RESUME) is None
    assert jev_score.stage1(ScriptedJudge(), "   ", RESUME) is None


def test_stage1_returns_none_when_the_judge_fails_and_names_only_the_class(capsys, monkeypatch):
    monkeypatch.setattr(jev_score, "_WARNED", set())
    judge = FailingJudge(ValueError("the request quoted the resume: SECRET-RESUME-TEXT"))
    assert jev_score.stage1(judge, JOB_MD, RESUME) is None
    assert jev_score.stage1(judge, JOB_MD, RESUME) is None
    out = capsys.readouterr().out
    assert out.count("ValueError") == 1 and "SECRET-RESUME-TEXT" not in out
    assert judge.calls == 2


def test_stage1_is_quiet_about_an_open_breaker(capsys, monkeypatch):
    """The run reports an outage once (score_jobs); each job's None stays silent."""
    monkeypatch.setattr(jev_score, "_WARNED", set())
    assert jev_score.stage1(FailingJudge(jev.JudgeOutage("TimeoutError")), JOB_MD, RESUME) is None
    assert capsys.readouterr().out == ""


def test_stage1_returns_none_for_an_empty_resume():
    judge = ScriptedJudge()
    assert jev_score.stage1(judge, JOB_MD, "  ") is None
    assert judge.calls == []


def test_stage_questions_pass_the_prompt_hygiene_census():
    import test_prompt_hygiene as hygiene
    texts = []

    def walk(value):
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(jev_score.CANDIDATE)
    walk(jev_score.stage1_questions())
    assert texts
    for text in texts:
        for label, pattern in hygiene.BANNED:
            assert not pattern.search(text), (label, text)


def test_stage1_trims_a_long_job_until_the_request_fits():
    judge = ScriptedJudge()
    huge = JOB_MD + ("Extra paragraph about the team and the office. " * 12000)
    assert not jev.request_fits({"candidate": jev_score.CANDIDATE, "resume": RESUME, "job": huge},
                                jev_score.stage1_questions())
    assert jev_score.stage1(judge, huge, RESUME) is not None
    state, questions = judge.calls[0]
    assert len(state["job"]) < len(huge)
    assert huge.startswith(state["job"])
    assert jev.request_fits(state, questions)


def test_stage1_gives_up_when_even_a_trimmed_request_cannot_fit():
    judge = ScriptedJudge()
    giant_resume = "Python " * 200000
    assert jev_score.stage1(judge, JOB_MD, giant_resume) is None
    assert judge.calls == []


def test_fitted_state_keeps_the_longest_job_text_jev_request_fits_passes(monkeypatch):
    limit = 5_000
    asked = []

    def request_fits(state, questions):
        asked.append(len(state["job"]))
        return len(state["job"]) <= limit
    monkeypatch.setattr(jev, "request_fits", request_fits)
    job = "word " * 4_000                         # 20,000 characters
    state = jev_score.fitted_state(job, RESUME, jev_score.stage1_questions())
    assert len(state["job"]) <= limit
    assert limit - jev_score.TRIM_STEP_CHARS <= len(state["job"])
    assert job.startswith(state["job"])
    assert len(asked) <= 20
    monkeypatch.setattr(jev, "request_fits", lambda state, questions: False)
    assert jev_score.fitted_state(job, RESUME, jev_score.stage1_questions()) is None


# --- SC-3: requirement lines ---------------------------------------------------------------

Req = jev_score.Req


def _texts(reqs):
    return [r.text for r in reqs]


def test_requirement_lines_prefer_the_requirement_heading():
    md = ("## About the role\nYou will join the analytics team.\n\n"
          "## Responsibilities\n* Build weekly dashboards\n* Present findings\n\n"
          "## Requirements\n* Python and SQL\n* Tableau or Power BI\n* Statistics coursework\n\n"
          "## Benefits\n* Health insurance\n* 401k match\n")
    reqs = jev_score.requirement_lines(md)
    assert _texts(reqs) == ["Python and SQL", "Tableau or Power BI", "Statistics coursework"]
    assert {r.must for r in reqs} == {True}


def test_requirement_lines_fill_from_other_bullets_when_the_headings_hold_too_few():
    md = ("## What you'll do\n- Query data in SQL\n- Build reports\n\n"
          "## Qualifications\n- Bachelor's degree in a quantitative field\n\n"
          "## Perks\n- Free lunch\n")
    assert _texts(jev_score.requirement_lines(md)) == [
        "Bachelor's degree in a quantitative field", "Query data in SQL", "Build reports"]


@pytest.mark.parametrize("heading,must", [
    ("## Requirements", True),
    ("### Minimum Qualifications", True),
    ("**Basic Qualifications**", True),
    ("**Required skills:**", True),
    ("What you must have:", True),
    ("## Qualifications", None),
    ("What you'll bring:", None),
    ("## Preferred Qualifications", False),
    ("**Nice to have**", False),
    ("Bonus points:", False),
    ("## What will make you stand out", False),
    # a heading that names both kinds, or an ideal candidate, leaves each line to Jev
    ("## Required and Preferred Qualifications", None),
    ("**Minimum and Preferred Qualifications**", None),
    ("The Ideal Candidate Will Have:", None),
])
def test_requirement_lines_read_the_heading_cue(heading, must):
    md = f"{heading}\n- Python and SQL\n- Tableau dashboards\n- Statistics coursework\n"
    reqs = jev_score.requirement_lines(md)
    assert _texts(reqs) == ["Python and SQL", "Tableau dashboards", "Statistics coursework"]
    assert {r.must for r in reqs} == {must}


def test_jev_must_read_decides_the_gaps_under_a_mixed_heading():
    md = ("## Required and Preferred Qualifications\n- Python and SQL\n- Tableau dashboards\n"
          "- Spark pipelines\n- Statistics coursework\n")
    judge = ScriptedJudge({"req_0_met": 0.9, "req_1_met": 0.1, "req_2_met": 0.1,
                           "req_3_met": 0.9, "req_1_must": 0.9, "req_2_must": 0.1})
    got = jev_score.stage2(judge, {"md": md, "facts": NO_FACTS}, RESUME)
    _state, questions = judge.calls[0]
    assert {f"req_{i}_must" for i in range(4)} <= set(questions)
    # Tableau: Jev reads it as a must-have; Spark: Jev reads it as preferred
    assert got["gaps"] == "Tableau dashboards"
    assert got["strengths"] == "Python and SQL | Statistics coursework"


def test_an_inline_preferred_cue_beats_a_required_heading():
    md = ("## Requirements\n- Python and SQL\n- Spark experience is a plus\n"
          "- dbt preferred\n- Airflow, ideally\n- Statistics coursework\n")
    got = {r.text: r.must for r in jev_score.requirement_lines(md)}
    assert got == {"Python and SQL": True, "Spark experience is a plus": False,
                   "dbt preferred": False, "Airflow, ideally": False,
                   "Statistics coursework": True}


@pytest.mark.parametrize("line", [
    "Must be located in the Chicago area",
    "Location: Austin, TX",
    "This role is on-site five days a week",
    "Onsite in our Denver office",
    "Hybrid schedule with 3 days in office",
    "Remote within the US",
    "Open to relocation to Seattle",
    "Willing to relocate",
    "Work in the Eastern time zone",
    "We cannot provide visa support",
    "No sponsorship available now or in the future",
    "Must have work authorization in the United States",
    "Authorized to work in the U.S. without sponsorship",
    "Must be a U.S. citizen",
    # eligibility lines that are no skill or tool (never a gap)
    "Able to pass a background check",
    "Must pass a drug screen",
    "Valid driver's license",
    "Able to lift up to 25 lbs",
    "Must be 18 years or older",
    "Up to 25% travel",
    "Available to work weekends and overtime",
    # the work-arrangement senses of hybrid, remote, travel, shift and commute
    "Hybrid or remote",
    "Fully remote",
    "Remote (US)",
    "Hybrid: 3 days a week at our Austin office",
    "Work remotely from anywhere in the US",
    "This is a hybrid role",
    "Travel up to 10% to client sites",
    "Willing to work night shifts",
    "Must live within commuting distance",
    "Valid driver’s license",
    "Candidates must be U.S. citizens",
    "We are unable to sponsor",
    # a leading arrangement word with a place, and travel written as a label
    "Travel: up to 25%",
    "Travel (25%)",
    "Travel - 25%",
    "Travel requirement: up to 20%",
    "Travel requirements are 10% of the time",
    "Willing to meet the travel requirements of the role",
    "Remote, US",
    "Remote US",
    "Remote/US",
    "Hybrid, Austin TX",
    "Open to remote candidates",
    # visa in its immigration wording
    "Must not require a work visa",
    "Visa status must allow full-time work",
    "H-1B visa holders are welcome",
])
def test_requirement_lines_drop_location_visa_and_eligibility_lines(line):
    md = f"## Requirements\n- Python and SQL\n- {line}\n- Tableau dashboards\n- Statistics\n"
    assert _texts(jev_score.requirement_lines(md)) == [
        "Python and SQL", "Tableau dashboards", "Statistics"]


@pytest.mark.parametrize("line", [
    "Hybrid cloud architecture on AWS and Azure",
    "Remote sensing and GIS analysis",
    "Hybrid search with vector databases",
    "Remote procedure calls with gRPC",
    "Location data analysis with PostGIS",
    "Travel industry analytics",
    "Work with project sponsors to define scope",
    "Experience supporting citizen data scientists",
    "Understanding of covariate shift and model drift",
    "Commutative algebra coursework",
    "18 years of analytics experience",
    "Visa and Mastercard payment data",
    "Remote user research and usability testing",
    "Hybrid/multi-cloud architecture",
    "Remote/edge device telemetry",
    "Travel requirements gathering for booking systems",
])
def test_requirement_lines_keep_skills_that_share_a_word_with_a_dropped_line(line):
    md = f"## Requirements\n- Python and SQL\n- {line}\n- Tableau dashboards\n"
    assert _texts(jev_score.requirement_lines(md)) == [
        "Python and SQL", line, "Tableau dashboards"]


def test_requirement_lines_skip_benefit_and_company_sections():
    md = ("## About Us\n- Founded in 1990\n- 500 employees\n\n"
          "## Requirements\n- SQL\n- Excel\n- Tableau\n\n"
          "## Compensation\n- $80,000 to $95,000\n\n"
          "## Equal Opportunity Employer\n- We welcome everyone\n")
    assert _texts(jev_score.requirement_lines(md)) == ["SQL", "Excel", "Tableau"]


def test_requirement_lines_read_numbers_bullets_and_markdown():
    md = ("**Requirements:**\n"
          "1. **Python** and [SQL](https://example.com/sql)\n"
          "2) Experience with node\\_js and C++\n"
          "• Tableau or Power BI;\n"
          "A sentence between the bullets\n"
          "    * Nested bullet about Git\n")
    assert _texts(jev_score.requirement_lines(md)) == [
        "Python and SQL", "Experience with node_js and C++", "Tableau or Power BI",
        "Nested bullet about Git"]


def test_requirement_lines_read_plain_lines_under_a_heading_with_no_bullets():
    md = ("Requirements:\nPython and SQL\nTableau or Power BI\nStatistics coursework\n"
          + ("A paragraph far too long to be one requirement. " * 6) + "\n\n"
          "About us:\nWe are a friendly team.\n")
    assert _texts(jev_score.requirement_lines(md)) == [
        "Python and SQL", "Tableau or Power BI", "Statistics coursework"]


def test_requirement_lines_drop_duplicates_intro_bullets_and_long_plain_paragraphs():
    md = ("## Requirements\n- Python\n- python\n- Experience with one of the following:\n"
          "- SQL\n- Excel\n" + ("A long paragraph about the team and its history. " * 8) + "\n")
    assert _texts(jev_score.requirement_lines(md)) == ["Python", "SQL", "Excel"]


def test_requirement_lines_keep_to_the_limit():
    md = "## Requirements\n" + "".join(f"- Skill number {i}\n" for i in range(40))
    assert len(jev_score.requirement_lines(md)) == 30
    assert _texts(jev_score.requirement_lines(md, limit=5)) == [
        f"Skill number {i}" for i in range(5)]


@pytest.mark.parametrize("desc", [None, "", "   ", 42, "Just a paragraph with no list at all."])
def test_requirement_lines_of_nothing_are_empty(desc):
    assert jev_score.requirement_lines(desc) == []


# --- SC-3: stage 2 composition ---------------------------------------------------------------

def _reqs(*musts):
    return [Req(f"Requirement {i}", m) for i, m in enumerate(musts)]


def _reads2(met, must=None, resp=1.0, seniority=1.0, domain=1.0):
    reads = {"responsibilities_fit": resp, "seniority_fit": seniority, "domain_fit": domain}
    for i, p in enumerate(met):
        reads[f"req_{i}_met"] = p
        reads[f"req_{i}_must"] = 0.9 if must is None else must[i]
    return reads


def test_stage2_everything_met_is_a_ten_to_apply():
    reqs = _reqs(True, True, True, False)
    got = jev_score.compose_stage2(reqs, _reads2([0.9, 0.9, 0.9, 0.9]))
    assert got["deep_score"] == 10 and got["recommendation"] == "apply"
    assert got["strengths"] == "Requirement 0 | Requirement 1 | Requirement 2 | Requirement 3"
    assert got["gaps"] == ""


def test_stage2_nothing_met_is_a_skip_with_the_must_haves_as_gaps():
    reqs = _reqs(True, True, False, True)
    got = jev_score.compose_stage2(reqs, _reads2([0.1] * 4, resp=0.0, seniority=0.0, domain=0.0))
    assert got["deep_score"] == 1 and got["recommendation"] == "skip"
    assert got["strengths"] == ""
    assert got["gaps"] == "Requirement 0 | Requirement 1 | Requirement 3"


def test_stage2_code_cue_wins_over_the_must_read():
    reqs = _reqs(False, True, None, None)
    reads = _reads2([0.1, 0.9, 0.1, 0.1], must=[0.95, 0.1, 0.9, 0.2])
    got = jev_score.compose_stage2(reqs, reads)
    # line 0: code says nice, so unmet it is no gap; line 2: Jev says must; line 3: Jev says nice
    assert got["gaps"] == "Requirement 2"
    assert got["strengths"] == "Requirement 1"


def test_stage2_strengths_put_must_haves_first_and_keep_five():
    reqs = _reqs(False, True, False, True, True, True, True, True)
    got = jev_score.compose_stage2(reqs, _reads2([0.9] * 8))
    assert got["strengths"].split(" | ") == [
        "Requirement 1", "Requirement 3", "Requirement 4", "Requirement 5", "Requirement 6"]


def test_stage2_gaps_keep_five():
    reqs = _reqs(*([True] * 8))
    got = jev_score.compose_stage2(reqs, _reads2([0.1] * 8))
    assert got["gaps"].split(" | ") == [f"Requirement {i}" for i in range(5)]


def test_stage2_lines_are_trimmed_and_keep_the_separator_free():
    long_text = "Experience with " + " ".join(f"tool{i}" for i in range(40)) + " | pipes"
    reqs = [Req(long_text, True), Req("SQL | Excel", True), Req("Tableau", True)]
    got = jev_score.compose_stage2(reqs, _reads2([0.9, 0.9, 0.1]))
    first, second = got["strengths"].split(" | ")
    assert len(first) <= jev_score.LINE_CHARS and first.endswith("...")
    assert long_text.startswith(first[:-3].rstrip())
    assert second == "SQL / Excel"


@pytest.mark.parametrize("fit,deep,rec", [
    (1.0, 10, "apply"),
    (0.70, 7, "apply"),
    (0.60, 6, "consider"),
    (0.45, 5, "consider"),
    (0.40, 5, "consider"),
    (0.30, 4, "skip"),
    (0.0, 1, "skip"),
])
def test_stage2_deep_score_maps_the_mix_to_one_to_ten(fit, deep, rec):
    reqs = _reqs(True, True, False)
    got = jev_score.compose_stage2(reqs, _reads2([fit] * 3, resp=fit, seniority=fit, domain=fit))
    assert (got["deep_score"], got["recommendation"]) == (deep, rec)


def test_stage2_weights_must_haves_most():
    reqs = _reqs(True, True, False, False)
    musts_met = jev_score.compose_stage2(reqs, _reads2([0.9, 0.9, 0.1, 0.1]))
    nice_met = jev_score.compose_stage2(reqs, _reads2([0.1, 0.1, 0.9, 0.9]))
    assert musts_met["deep_score"] > nice_met["deep_score"]


def test_stage2_without_nice_to_haves_reweights_the_rest():
    reqs = _reqs(True, True, True)
    got = jev_score.compose_stage2(reqs, _reads2([1.0, 1.0, 1.0]))
    assert got["deep_score"] == 10


def test_stage2_asks_the_must_read_only_for_a_line_the_code_gives_no_cue():
    reqs = [Req("Python and SQL", True), Req("Tableau", None), Req("Spark is a plus", False)]
    qs = jev_score.stage2_questions(reqs)
    assert set(qs) == {"req_0_met", "req_1_met", "req_1_must", "req_2_met",
                       "responsibilities_fit", "seniority_fit", "domain_fit"}
    assert all(qs[k]["type"] == "noul" for k in ("req_0_met", "req_1_met", "req_1_must",
                                                  "req_2_met"))
    assert all(qs[k]["type"] == "score" for k in ("responsibilities_fit", "seniority_fit",
                                                   "domain_fit"))
    assert "Python and SQL" in qs["req_0_met"]["instructions"]
    assert "`resume`" in qs["req_0_met"]["instructions"]
    assert "Tableau" in qs["req_1_must"]["instructions"]
    assert "`job`" in qs["req_1_must"]["instructions"]
    assert "`candidate`" in qs["seniority_fit"]["instructions"]


def test_stage2_reads_and_composes_without_must_answers_for_cued_lines():
    reqs = [Req("Python and SQL", True), Req("Tableau", None), Req("Spark is a plus", False)]
    qs = jev_score.stage2_questions(reqs)
    answers = ScriptedJudge({"req_0_met": 0.2, "req_1_met": 0.2, "req_1_must": 0.1,
                             "req_2_met": 0.2}).judge({}, qs)
    reads = jev_score.stage2_reads(answers, qs, len(reqs))
    assert reads is not None
    assert set(reads) == set(qs)
    assert jev_score.compose_stage2(reqs, reads)["gaps"] == "Python and SQL"


def test_stage2_questions_pass_the_prompt_hygiene_census():
    import test_prompt_hygiene as hygiene
    qs = jev_score.stage2_questions([Req("Python and SQL", True)])
    texts = []
    for q in qs.values():
        texts.append(q["instructions"])
        crit = q["criteria"]
        texts.extend(crit.values() if isinstance(crit, dict) else crit)
    for text in texts:
        for label, pattern in hygiene.BANNED:
            assert not pattern.search(text), (label, text)


JOB2_MD = ("## Responsibilities\n- Build dashboards\n\n"
           "## Requirements\n- Python and SQL\n- Tableau or Power BI\n- Statistics coursework\n"
           "\n## Preferred Qualifications\n- Spark\n")


def test_stage2_sends_one_request_and_composes_the_columns():
    judge = ScriptedJudge({"req_0_met": 0.9, "req_1_met": 0.8, "req_2_met": 0.2,
                           "req_3_met": 0.1, "req_3_must": 0.9,
                           "responsibilities_fit": 3, "seniority_fit": 3, "domain_fit": 3})
    got = jev_score.stage2(judge, {"md": JOB2_MD, "facts": NO_FACTS}, RESUME)
    assert len(judge.calls) == 1
    state, questions = judge.calls[0]
    assert set(state) == {"candidate", "resume", "job"}
    assert len(questions) == 4 + 3        # every line has a heading cue: no must question
    assert set(got) == {"deep_score", "strengths", "gaps", "recommendation"}
    assert got["strengths"] == "Python and SQL | Tableau or Power BI"
    assert got["gaps"] == "Statistics coursework"       # Spark: the heading says preferred
    assert 1 <= got["deep_score"] <= 10
    assert got["recommendation"] in ("apply", "consider", "skip")


def test_stage2_with_fewer_than_three_lines_takes_the_llm_path():
    judge = ScriptedJudge()
    md = "## Requirements\n- Python\n- SQL\n\nA paragraph about the team."
    assert jev_score.stage2(judge, md, RESUME) is None
    assert judge.calls == []


@pytest.mark.parametrize("table", [
    {"req_1_met": 1.5},
    {"seniority_fit": 7},
    {"domain_fit": float("inf")},
])
def test_stage2_returns_none_on_a_read_it_cannot_use(table):
    assert jev_score.stage2(ScriptedJudge(table), JOB2_MD, RESUME) is None


def test_stage2_returns_none_without_a_judge_or_when_it_fails(monkeypatch):
    monkeypatch.setattr(jev_score, "_WARNED", set())
    assert jev_score.stage2(None, JOB2_MD, RESUME) is None
    assert jev_score.stage2(FailingJudge(RuntimeError("boom")), JOB2_MD, RESUME) is None
    assert jev_score.stage2(ScriptedJudge(), JOB2_MD, "") is None


# --- SC-4, SC-5: the hooks in score_jobs.py ---------------------------------------------------

def _sj():
    import score_jobs
    return score_jobs


def _llm_response(text):
    from types import SimpleNamespace
    return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(
        prompt_token_count=1, candidates_token_count=1))


class RecordingPool:
    """The LLM provider: records (stage, job text) and answers like the Gemini path."""

    def __init__(self, score=5):
        self.score = score
        self.calls = []

    async def generate(self, *, model, contents, config):
        sj = _sj()
        stage = 1 if model == sj.STAGE1_MODELS else 2
        self.calls.append((stage, model, contents))
        if stage == 1:
            return _llm_response(json.dumps({"score": self.score, "reason": "llm reason"}))
        return _llm_response(json.dumps({"deep_score": 8, "strengths": ["llm strength"],
                                         "gaps": ["llm gap"], "recommendation": "consider"}))

    def stats(self):
        return {"free_calls": len(self.calls), "vertex_calls": 0}

    def jobs(self, stage):
        return sorted(tag for s, _m, text in self.calls if s == stage
                      for tag in ("JOB-A", "JOB-B", "JOB-C", "JOB-D") if tag in text)


def _jobs_df(*tags, md=JOB2_MD):
    return pd.DataFrame({
        "job_posting_id": [t.lower() for t in tags],
        "job_description_md": [f"{t}\n{md}" for t in tags],
        "filtered_out": [False] * len(tags),
    })


def test_run_scoring_scores_both_stages_with_jev_and_never_calls_the_llm():
    sj = _sj()
    pool = RecordingPool()
    run = sj.JevRun(ScriptedJudge())
    merged = asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A", "JOB-B"), jev_run=run))
    assert pool.calls == []
    assert list(merged["score"]) == [5, 5]
    assert set(merged["reason"]) == {"Skills fit strong (1.00); domain data science or ML; "
                                     "no experience level stated; no degree bar"}
    assert list(merged["deep_score"]) == [10, 10]
    assert set(merged["recommendation"]) == {"apply"}
    assert merged["strengths"].iloc[0].startswith("Python and SQL | ")
    assert (run.scored, run.fallback) == ({1: 2, 2: 2}, {1: 0, 2: 0})


def test_run_scoring_sends_the_code_facts_with_stage_one(monkeypatch):
    sj = _sj()
    seen = []

    def fake_stage1(judge, job, resume):
        seen.append(job["facts"])
        return {"score": 4, "reason": "r"}

    monkeypatch.setattr(jev_score, "stage1", fake_stage1)
    monkeypatch.setattr(jev_score, "stage2", lambda judge, job, resume: None)
    md = "Requires 3+ years of experience. A Master's degree is required.\n" + JOB2_MD
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-A", md=md),
                               jev_run=sj.JevRun(ScriptedJudge())))
    assert seen == [{"min_years": 3, "advanced_degree": True, "clearance": False}]


def test_a_job_jev_cannot_score_takes_the_llm_path():
    sj = _sj()
    pool = RecordingPool()
    run = sj.JevRun(ScriptedJudge())
    df = pd.concat([_jobs_df("JOB-A"), _jobs_df("JOB-B", md="A short posting with no list.")],
                   ignore_index=True)
    merged = asyncio.run(sj.run_scoring(pool, RESUME, df, jev_run=run)).set_index("job_posting_id")
    # JOB-B has too few requirement lines for Jev's stage 2: the LLM writes it
    assert pool.jobs(1) == [] and pool.jobs(2) == ["JOB-B"]
    assert merged.loc["job-b", "recommendation"] == "consider"
    assert merged.loc["job-a", "recommendation"] == "apply"
    assert (run.scored, run.fallback) == ({1: 2, 2: 1}, {1: 0, 2: 1})


class FailsAfter(ScriptedJudge):
    """Answers `ok` requests, then the service goes away."""

    def __init__(self, ok):
        super().__init__()
        self.ok = ok

    def judge(self, state, questions):
        if len(self.calls) >= self.ok:
            self.calls.append((state, questions))
            raise ConnectionError("service gone")
        return super().judge(state, questions)


def test_an_outage_mid_run_moves_the_rest_of_the_run_to_the_llm(monkeypatch, capsys):
    sj = _sj()
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 1)
    inner = FailsAfter(ok=1)
    judge = jev.Guarded(inner, sleep=lambda s: None, delays=())
    run = sj.JevRun(judge)
    pool = RecordingPool(score=5)
    df = _jobs_df("JOB-A", "JOB-B", "JOB-C", "JOB-D")
    merged = asyncio.run(sj.run_scoring(pool, RESUME, df, jev_run=run)).set_index("job_posting_id")
    assert judge.down == "ConnectionError"
    assert len(inner.calls) == 2                  # one answer, one failure, then no more requests
    assert pool.jobs(1) == ["JOB-B", "JOB-C", "JOB-D"]
    assert pool.jobs(2) == ["JOB-A", "JOB-B", "JOB-C", "JOB-D"]
    assert merged.loc["job-a", "reason"].startswith("Skills fit")
    assert merged.loc["job-b", "reason"] == "llm reason"
    assert not merged["reason"].astype(str).str.startswith("ERROR").any()
    assert (run.scored, run.fallback) == ({1: 1, 2: 0}, {1: 3, 2: 4})
    out = capsys.readouterr().out
    assert out.count("Jev is unavailable (ConnectionError)") == 1


def test_jev_calls_run_in_worker_threads_under_their_own_semaphore(monkeypatch):
    import threading
    import time
    sj = _sj()
    monkeypatch.setattr(sj, "JEV_CONCURRENCY", 2)
    lock = threading.Lock()
    live = {"now": 0, "max": 0, "threads": set()}

    class Slow(ScriptedJudge):
        def judge(self, state, questions):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                live["threads"].add(threading.current_thread() is threading.main_thread())
            time.sleep(0.05)
            with lock:
                live["now"] -= 1
            return super().judge(state, questions)

    df = _jobs_df("JOB-A", "JOB-B", "JOB-C", "JOB-D")
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, df, jev_run=sj.JevRun(Slow())))
    assert live["max"] == 2
    assert live["threads"] == {False}


def test_a_jev_request_leaves_the_default_executor_to_the_llm_calls(monkeypatch):
    """A Jev request, retry sleeps included, holds one of the run's own worker
    threads, so ClaudePool's calls (asyncio.to_thread, the default executor)
    never wait behind it."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    sj = _sj()
    started, release = threading.Event(), threading.Event()
    names = []

    def held_stage1(judge, job, resume):
        names.append(threading.current_thread().name)
        started.set()
        release.wait(timeout=10)            # a retry sleep in progress
        return {"score": 4, "reason": "r"}

    monkeypatch.setattr(jev_score, "stage1", held_stage1)

    async def go():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        run = sj.JevRun(ScriptedJudge())
        jev_task = asyncio.ensure_future(run.ask(asyncio.Semaphore(1), 1, "job-x", {"md": "x"},
                                                 RESUME))
        try:
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            llm = await asyncio.wait_for(asyncio.to_thread(lambda: "llm answer"), timeout=2)
        finally:
            release.set()
        got = await jev_task
        run.close()
        return llm, got

    assert asyncio.run(go()) == ("llm answer", {"score": 4, "reason": "r"})
    assert names and names[0].startswith("jev")


def test_main_lets_the_jev_worker_threads_go_once_the_run_is_done(monkeypatch):
    sj = _sj()
    closed = []
    monkeypatch.setattr(sj.JevRun, "close", lambda self: closed.append(self))
    seen = _main_stubs(monkeypatch, sj, ScriptedJudge())
    asyncio.run(sj.main())
    assert closed == [seen["rescore_jev"]]


def test_jev_on_without_an_llm_provider_keeps_error_rows_and_scores_only(monkeypatch):
    sj = _sj()
    monkeypatch.setattr(jev_score, "_WARNED", set())

    class RefusesB(ScriptedJudge):
        def judge(self, state, questions):
            if "JOB-B" in state["job"]:
                raise RuntimeError("no answer for this one")
            return super().judge(state, questions)

    run = sj.JevRun(RefusesB())
    df = pd.concat([_jobs_df("JOB-A", md="A short posting with no list."), _jobs_df("JOB-B")],
                   ignore_index=True)
    merged = asyncio.run(sj.run_scoring(None, RESUME, df, jev_run=run)).set_index("job_posting_id")
    assert merged.loc["job-a", "score"] == 5
    assert pd.isna(merged.loc["job-a", "deep_score"])          # scores only
    assert pd.isna(merged.loc["job-b", "score"])
    assert merged.loc["job-b", "reason"] == sj.NO_LLM_REASON
    assert sj.NO_LLM_REASON.startswith("ERROR:")                # the rescore pass retries it
    assert list(sj.rows_needing_rescore(merged.reset_index())["job_posting_id"]) == ["job-b"]
    assert (run.no_llm_errors, run.scores_only) == (1, 1)


class Refuses(ScriptedJudge):
    """Raises for a job whose text holds one of `tags` and answers the rest."""

    def __init__(self, *tags):
        super().__init__()
        self.tags = set(tags)

    def judge(self, state, questions):
        if any(tag in state["job"] for tag in self.tags):
            raise RuntimeError("no answer for this one")
        return super().judge(state, questions)


def test_a_job_both_passes_see_counts_once_per_stage(monkeypatch):
    """The rescore pass hands `run_scoring` the run's JevRun again, and the fresh
    pass's ERROR rows are among the rows it retries. A job that falls back on the
    fresh pass and again on the rescore pass counts once; so does a job Jev
    scores on both."""
    sj = _sj()
    monkeypatch.setattr(jev_score, "_WARNED", set())
    run = sj.JevRun(Refuses("JOB-B"))
    for _pass in ("fresh", "rescore"):
        asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-A", "JOB-B"),
                                   jev_run=run))
    run.close()
    assert (run.scored, run.fallback) == ({1: 1, 2: 1}, {1: 1, 2: 1})
    assert run.stats()["jev_stage1_fallback"] == 1


def test_a_jev_score_on_the_rescore_pass_beats_the_fresh_pass_fallback(monkeypatch):
    sj = _sj()
    monkeypatch.setattr(jev_score, "_WARNED", set())
    judge = Refuses("JOB-B")
    run = sj.JevRun(judge)
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-B"), jev_run=run))
    assert (run.scored, run.fallback) == ({1: 0, 2: 0}, {1: 1, 2: 1})
    judge.tags.clear()                          # Jev answers on the rescore pass
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-B"), jev_run=run))
    run.close()
    assert (run.scored, run.fallback) == ({1: 1, 2: 1}, {1: 0, 2: 0})


def test_an_error_row_without_an_llm_provider_counts_once_across_both_passes(monkeypatch):
    sj = _sj()
    monkeypatch.setattr(jev_score, "_WARNED", set())
    judge = Refuses("JOB-B")
    run = sj.JevRun(judge)
    for _pass in ("fresh", "rescore"):
        asyncio.run(sj.run_scoring(None, RESUME, _jobs_df("JOB-B"), jev_run=run))
    assert (run.no_llm_errors, run.fallback[1]) == (1, 1)
    judge.tags.clear()                          # a later pass that Jev answers
    asyncio.run(sj.run_scoring(None, RESUME, _jobs_df("JOB-B"), jev_run=run))
    run.close()
    assert (run.no_llm_errors, run.scored[1], run.fallback[1]) == (0, 1, 0)


def test_with_jev_off_run_scoring_is_the_llm_path_as_before():
    sj = _sj()
    pool = RecordingPool()
    df = _jobs_df("JOB-A", "JOB-B")
    for run in (None, sj.JevRun(None)):
        pool.calls.clear()
        merged = asyncio.run(sj.run_scoring(pool, RESUME, df, jev_run=run))
        assert pool.jobs(1) == ["JOB-A", "JOB-B"] and pool.jobs(2) == ["JOB-A", "JOB-B"]
        assert set(merged["reason"]) == {"llm reason"}


def test_the_run_summary_counts_jev_requests_spend_and_fallbacks_by_stage():
    sj = _sj()
    run = sj.JevRun(jev.Guarded(jev.DryRun(ScriptedJudge())))
    df = pd.concat([_jobs_df("JOB-A"), _jobs_df("JOB-B", md="A short posting with no list.")],
                   ignore_index=True)
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, df, jev_run=run))
    stats = run.stats()
    assert (stats["jev_stage1_scored"], stats["jev_stage2_scored"], stats["jev_requests"],
            stats["jev_stage1_fallback"], stats["jev_stage2_fallback"]) == (2, 1, 3, 0, 1)
    assert 0 < stats["jev_usd"] < 0.01
    # each count is jobs at one stage: JOB-B's stage 2 took the LLM path
    assert run.summary_line() == (f"Jev scored stage 1: 2, stage 2: 1 (3 requests, "
                                  f"${stats['jev_usd']:.4f}); LLM fallback stage 1: 0, "
                                  "stage 2: 1")


def test_run_stats_carry_the_jev_columns(tmp_path, monkeypatch):
    sj = _sj()
    assert sj.RUN_STATS_COLS[-6:] == ["jev_stage1_scored", "jev_stage2_scored", "jev_requests",
                                      "jev_usd", "jev_stage1_fallback", "jev_stage2_fallback"]
    monkeypatch.setattr(sj, "RUN_STATS_CSV", tmp_path / "run_stats.csv")
    sj.append_run_stats({"jev_stage1_scored": 7, "jev_stage2_scored": 4, "jev_requests": 11,
                         "jev_usd": 0.0021, "jev_stage1_fallback": 2, "jev_stage2_fallback": 1})
    row = pd.read_csv(tmp_path / "run_stats.csv").iloc[0]
    assert (row["jev_stage1_scored"], row["jev_stage2_scored"], row["jev_requests"],
            row["jev_stage1_fallback"], row["jev_stage2_fallback"]) == (7, 4, 11, 2, 1)
    assert row["jev_usd"] == pytest.approx(0.0021)


def test_jev_run_off_reports_zeros():
    sj = _sj()
    assert sj.JevRun(None).stats() == {
        "jev_stage1_scored": 0, "jev_stage2_scored": 0, "jev_requests": 0, "jev_usd": 0.0,
        "jev_stage1_fallback": 0, "jev_stage2_fallback": 0}


def test_make_jev_judge_on_the_vm_stays_on_the_llm(monkeypatch, capsys):
    """No SCORE_USE_JEV and no dashboard config beside the scorer (the VM's flat
    copy): no judge, and one line saying why, with no warning."""
    sj = _sj()
    monkeypatch.delenv("SCORE_USE_JEV", raising=False)
    monkeypatch.setattr(jev_score, "CONFIG_PATH", None)
    assert sj.make_jev_judge() is None
    assert capsys.readouterr().out == (
        "Jev scoring off (no SCORE_USE_JEV and no dashboard config beside the scorer).\n")


@pytest.mark.parametrize("env,cfg,why", [
    ({"SCORE_USE_JEV": "0"}, {}, "SCORE_USE_JEV=0 turns it off"),
    ({}, {"jev_enabled": False}, "Jev is switched off in Settings"),
    ({}, {"jev_scoring": False}, "Jev is switched off for scoring in Settings"),
])
def test_make_jev_judge_says_in_one_line_why_scoring_stays_off(monkeypatch, capsys, cfg_file,
                                                               sdk, env, cfg, why):
    sj = _sj()
    cfg_file(cfg)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert sj.make_jev_judge() is None
    assert capsys.readouterr().out == f"Jev scoring off ({why}).\n"


def test_make_jev_judge_leaves_a_missing_key_to_the_one_warning(monkeypatch, capsys, cfg_file,
                                                                sdk):
    sj = _sj()
    cfg_file({})
    assert sj.make_jev_judge() is None
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("WARNING: Jev scoring is on (Settings)")
    assert "Jev scoring off" not in out


def test_make_jev_judge_builds_a_guarded_judge_when_the_switch_is_on(monkeypatch, capsys, sdk):
    sj = _sj()

    class Live:
        def judge(self, state, questions):
            raise AssertionError("no request in this test")

    monkeypatch.setenv("SCORE_USE_JEV", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setattr(jev, "TypeSafeJev", Live)
    judge = sj.make_jev_judge()
    assert isinstance(judge, jev.Guarded) and isinstance(judge.inner, Live)
    # The short retries jev_switch.client gives scoring: an outage reaches the
    # LLM path in seconds, since the batch has that path to fall back on.
    assert judge.delays == jev.QUICK_RETRY_DELAYS_S
    out = capsys.readouterr().out
    assert "Jev scoring on (SCORE_USE_JEV=1)" in out and "not-a-real-key" not in out


def _main_stubs(monkeypatch, sj, judge):
    from types import SimpleNamespace
    seen = {}

    def make_pool(required=True):
        seen["required"] = required
        return None if not required else RecordingPool()

    async def rescore(pool, resume, *, jev_run=None):
        seen["rescore_jev"] = jev_run
        return 0, 0

    monkeypatch.setattr(sj, "parse_args", lambda: SimpleNamespace(csv=None))
    monkeypatch.setattr(sj, "load_resume", lambda: RESUME)
    monkeypatch.setattr(sj, "latest_input_csv", lambda: None)
    monkeypatch.setattr(sj, "make_jev_judge", lambda: judge)
    monkeypatch.setattr(sj, "make_pool", make_pool)
    monkeypatch.setattr(sj, "rescore_master_failures", rescore)
    monkeypatch.setattr(sj, "append_run_stats", lambda stats: seen.setdefault("stats", stats))
    return seen


def test_main_with_jev_on_needs_no_llm_provider_and_logs_the_summary(monkeypatch, capsys):
    sj = _sj()
    seen = _main_stubs(monkeypatch, sj, ScriptedJudge())
    asyncio.run(sj.main())
    assert seen["required"] is False
    assert seen["rescore_jev"].judge is not None
    assert seen["stats"]["free_calls"] == 0 and seen["stats"]["jev_stage1_scored"] == 0
    assert ("Jev scored stage 1: 0, stage 2: 0 (0 requests, $0.0000); "
            "LLM fallback stage 1: 0, stage 2: 0") in capsys.readouterr().out


def test_main_with_jev_off_needs_the_llm_provider_as_today(monkeypatch, capsys):
    sj = _sj()
    seen = _main_stubs(monkeypatch, sj, None)
    asyncio.run(sj.main())
    assert seen["required"] is True
    assert seen["stats"]["jev_stage1_scored"] == 0 and seen["stats"]["jev_stage2_fallback"] == 0
    assert "Jev scored" not in capsys.readouterr().out


def test_make_pool_without_credentials_exits_only_when_it_is_required(monkeypatch, capsys):
    sj = _sj()

    def no_creds(cls, *, state_path, limits=None):
        raise sj.PoolError("No Gemini credentials")

    monkeypatch.setattr(sj.KeyPool, "from_env", classmethod(no_creds))
    assert sj.make_pool(required=False) is None
    assert "No Gemini credentials" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        sj.make_pool()
