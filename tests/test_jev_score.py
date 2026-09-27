"""SC-1 to SC-3 and JS-4 (cycle 19): the Jev scorer's switch, its questions,
the requirement-line extractor and the composition code.

Hermetic: the dashboard config is a file in tmp_path (monkeypatched onto
`jev_score.CONFIG_PATH`), the environment is a dict, the SDK probe is patched,
and every judge is scripted. No key, no network.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def test_a_flat_copy_has_no_config_path(tmp_path, sdk, capsys):
    """The VM copies the pipeline scripts flat into ~/, where there is no
    local/config.json beside them: CONFIG_PATH is None there, so Jev is off."""
    shutil.copy(REPO / "pipeline" / "jev_score.py", tmp_path / "jev_score.py")
    spec = importlib.util.spec_from_file_location("jev_score_flat", tmp_path / "jev_score.py")
    flat = importlib.util.module_from_spec(spec)
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
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=90)
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
