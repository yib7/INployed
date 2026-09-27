"""jev_assist: the tailor's Jev requests (TL-1 to TL-9).

Each helper asks the judge one kind of question and hands Jev's answers back as
data; its caller composes. The helpers run here against the key-free FakeJev,
against scripted judges where the exact probabilities matter, and against
judges that fail: a helper returns None when Jev is off, when there is nothing
to ask, when a request fails or comes back malformed, and once jev.Guarded's
breaker is open, so its caller keeps the path it had before cycle 19.

No request leaves the process: every judge here is a fake, and the conftest
drops TYPESAFE_API_KEY, so `jev_switch.client("tailor")` is None unless a test
patches it.
"""
import logging
import re
import sys
import textwrap
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
import jev_switch  # noqa: E402
from resume_tailor import assets, compose, config, jev_assist  # noqa: E402

_CACHED = (assets.load_master, assets.tailor_config, assets.atoms_by_id, assets.blocks,
           assets.skill_aliases, assets.skill_aliases_match_only)

_MASTER = textwrap.dedent("""
    basics:
      name: Sam Rivera
      email: sam@example.com
    experience:
      - org: Acme Data
        title: Analyst Intern
        dates: "2024-06 / 2024-08"
        achievements:
          - {id: ac_sql, what: "Wrote SQL reports on warehouse sales data", angles: [data]}
          - {id: ac_dash, what: "Built a sales dashboard for regional managers", angles: [data]}
    projects:
      - name: Orbit
        dates: "2024-01 / 2024-05"
        achievements:
          - {id: or_api, what: "Served pass times through a Flask API with Redis caching",
             angles: [backend]}
          - {id: or_overview, what: "Orbit is a satellite pass predictor for radio operators",
             angles: [product]}
      - name: Pantry
        dates: "2023-09 / 2023-12"
        achievements:
          - {id: pa_overview, what: "Pantry is a meal planner that tracks fridge contents",
             angles: [product]}
    leadership:
      - org: Chess Club
        dates: "2022-09 / 2024-05"
        achievements:
          - {id: cc_lead, what: "Led weekly training sessions for 20 members", angles: [lead]}
    skills:
      languages: [Python, SQL, Go]
      frameworks: [Flask]
      developer_tools: [Git, Docker, Redis]
      libraries: [pandas]
""")

_JD = ("Data Analyst. You will write SQL against our warehouse, build dashboards for "
       "sales leaders and automate weekly reports in Python. Experience with pandas "
       "and Git helps.")
_TITLE = "Data Analyst"
_POOL_ORDER = ["Python", "SQL", "Go", "Flask", "Git", "Docker", "Redis", "pandas"]
_WHATS = ["Wrote SQL reports on warehouse sales data",
          "Built a sales dashboard for regional managers",
          "Served pass times through a Flask API with Redis caching",
          "Orbit is a satellite pass predictor for radio operators",
          "Pantry is a meal planner that tracks fridge contents",
          "Led weekly training sessions for 20 members"]
_PROJECTS = [{"project": "Orbit",
              "bullets": ["Served pass times through a Flask API with Redis caching",
                          "Orbit is a satellite pass predictor for radio operators"]}]


@pytest.fixture()
def master(tmp_path, monkeypatch):
    p = tmp_path / "master_experience.yaml"
    p.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    for fn in _CACHED:
        fn.cache_clear()
    jev_assist.reset_usage()
    yield p
    for fn in _CACHED:
        fn.cache_clear()


# ── fake judges ──────────────────────────────────────────────────────────────
class Recording:
    """Wraps a judge and keeps every request it was sent."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        return self.inner.judge(state, questions)


def _item(state, text, key):
    """The state item a question's backticked `key[i]` path names."""
    m = re.search(r"`%s\[(\d+)\]`" % key, text)
    return state[key][int(m.group(1))]


class Scripted:
    """Answers nouls with `noul(state, instructions)` and every choice with `choice`
    (the first option when that is not one of them) at `confidence`."""

    def __init__(self, noul=lambda state, text: 0.5, choice=None, confidence=1.0):
        self.noul = noul
        self.choice = choice
        self.confidence = confidence
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        out = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                out[qid] = jev.Answer(kind="noul", noul=self.noul(state, q["instructions"]))
            else:
                names = list(q["criteria"])
                pick = self.choice if self.choice in names else names[0]
                out[qid] = jev.Answer(kind="choice", choice=pick,
                                      probabilities={n: float(n == pick) for n in names},
                                      confidence=self.confidence)
        return out


class Failing:
    """A judge whose every request raises, as a rejected request or a bug does."""

    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        raise RuntimeError("the request was rejected, with detail that must not be logged")


class _Busy(Exception):
    status = 503


class Down:
    """A judge the service cannot answer (503 every time)."""

    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        raise _Busy("service unavailable")


def _skills(judge):
    return jev_assist.skills_pick(_JD, _TITLE, judge=judge)


def _atoms(judge):
    return jev_assist.atom_relevance(_JD, _TITLE, judge=judge)


def _lead(judge):
    return jev_assist.lead_group(_PROJECTS, judge=judge)


# TL-4's entries: each bullet with the atoms it was written from, as the run hands
# them over (the atoms as `compose._atom_payload` gives them).
_CHESS = {"entry": "Chess Club", "bullets": [
    {"gkey": "cc_lead", "text": "Led weekly training sessions for 20 members.",
     "atoms": [{"id": "cc_lead", "what": "Led weekly training sessions for 20 members",
                "angles": ["lead"]}]}]}
_ACME = {"entry": "Acme Data", "bullets": [
    {"gkey": "ac_sql", "text": "Wrote SQL reports on warehouse sales data.",
     "atoms": [{"id": "ac_sql", "what": "Wrote SQL reports on warehouse sales data",
                "angles": ["data"]}]},
    {"gkey": "ac_dash", "text": "Built a sales dashboard for regional managers.",
     "atoms": [{"id": "ac_dash", "what": "Built a sales dashboard for regional managers",
                "angles": ["data"]}]}]}


def _faith(judge):
    return jev_assist.faithfulness([_CHESS], judge=judge)


# TL-6's options: the palette's unused verbs, each with its category, the category
# holding the repeated verb first.
_VERB_BULLET = "Built a sales model to analyze regional demand."
_VERB_OPTIONS = {"Designed": "Build", "Modeled": "Analyze", "Coordinated": "Lead"}


def _verb(judge):
    return jev_assist.pick_verb(_VERB_BULLET, _VERB_OPTIONS, judge=judge)


# TL-5's entries: each bullet's text, as the sweep hands them over.
def _plain(entry):
    return {"entry": entry["entry"],
            "bullets": [{"gkey": b["gkey"], "text": b["text"]} for b in entry["bullets"]]}


def _sweep(judge):
    return jev_assist.sweep_flags([_plain(_CHESS)], judge=judge)


# TL-7's groups: each bullet's drafts that passed the gate and TL-4.
_DRAFTS = [{"gkey": "ac_sql", "drafts": [
    "Wrote SQL reports on warehouse sales data.",
    "Wrote weekly SQL reports on warehouse sales data for sales leaders."]}]


def _best(judge):
    return jev_assist.best_variant(_JD, _TITLE, _DRAFTS, judge=judge)


# TL-8's input: the letter's sentences and its sources, the job left out.
_SENTENCES = ["At Acme Data I wrote SQL reports on warehouse sales data.",
              "I led the analytics team through a warehouse migration."]
_SOURCES = {"resume bullets": ["Wrote SQL reports on warehouse sales data."],
            "background": "Acme Data, Analyst Intern: wrote SQL reports on warehouse "
                          "sales data; built a sales dashboard for regional managers.",
            "own words": "", "basics": "Sam Rivera. Education: BS Statistics."}


def _letter(judge):
    return jev_assist.letter_unsupported(_SENTENCES, _SOURCES, judge=judge)


# TL-9's input: the report's keywords and the résumé's text.
_KEYWORDS = ["sql", "dashboards", "kubernetes"]
_RESUME = "Wrote SQL reports on warehouse sales data. Built a sales dashboard."


def _meaning(judge):
    return jev_assist.keyword_meaning(_KEYWORDS, _RESUME, judge=judge)


_HELPERS = [pytest.param(_skills, id="skills_pick"), pytest.param(_atoms, id="atom_relevance"),
            pytest.param(_lead, id="lead_group"), pytest.param(_faith, id="faithfulness"),
            pytest.param(_verb, id="pick_verb"), pytest.param(_sweep, id="sweep_flags"),
            pytest.param(_best, id="best_variant"),
            pytest.param(_letter, id="letter_unsupported"),
            pytest.param(_meaning, id="keyword_meaning")]


# ── off, default judge, failures ─────────────────────────────────────────────
def test_every_helper_is_none_and_asks_nothing_when_jev_is_off(master, monkeypatch):
    """No key in the suite, so the real client is None; patched here to say so
    outright and to prove each helper asked it."""
    areas = []

    def client(area):
        areas.append(area)
        return None

    monkeypatch.setattr(jev_switch, "client", client)
    assert jev_assist.skills_pick(_JD, _TITLE) is None
    assert jev_assist.atom_relevance(_JD, _TITLE) is None
    assert jev_assist.lead_group(_PROJECTS) is None
    assert jev_assist.faithfulness([_CHESS]) is None
    assert jev_assist.pick_verb(_VERB_BULLET, _VERB_OPTIONS) is None
    assert jev_assist.sweep_flags([_plain(_CHESS)]) is None
    assert jev_assist.best_variant(_JD, _TITLE, _DRAFTS) is None
    assert jev_assist.letter_unsupported(_SENTENCES, _SOURCES) is None
    assert jev_assist.keyword_meaning(_KEYWORDS, _RESUME) is None
    assert areas == ["tailor"] * 9


def test_the_suite_default_client_is_off(master):
    assert jev_assist.default_judge() is None
    assert jev_assist.skills_pick(_JD, _TITLE) is None


@pytest.mark.parametrize("helper", _HELPERS)
def test_the_default_judge_is_the_tailor_client(master, monkeypatch, helper):
    fake = Recording(jev.FakeJev())
    areas = []

    def client(area):
        areas.append(area)
        return fake

    monkeypatch.setattr(jev_switch, "client", client)
    assert helper(jev_assist._DEFAULT) is not None
    assert areas == ["tailor"]
    assert len(fake.requests) == 1


@pytest.mark.parametrize("helper", _HELPERS)
def test_judge_none_means_off_without_building_a_client(master, monkeypatch, helper):
    # default_judge() swallows any error the switch raises, so a raising guard would
    # pass unseen; the client requests are counted instead.
    areas = []
    monkeypatch.setattr(jev_switch, "client", lambda area: areas.append(area) or jev.FakeJev())
    assert helper(None) is None
    assert areas == [], "judge=None built a client"


def test_a_client_that_cannot_be_built_reads_as_off(monkeypatch):
    def client(area):
        raise RuntimeError("config.json is broken")

    monkeypatch.setattr(jev_switch, "client", client)
    assert jev_assist.default_judge() is None


@pytest.mark.parametrize("helper", _HELPERS)
def test_a_failing_judge_returns_none(master, helper):
    judge = Failing()
    assert helper(judge) is None
    assert judge.calls == 1


_LLM_PATH = "the LLM path"
_GATE_ALONE = "the deterministic gate alone"


@pytest.mark.parametrize("helper,step,fallback", [
    pytest.param(_skills, jev_assist.STEP_SKILLS, _LLM_PATH, id="skills_pick"),
    pytest.param(_atoms, jev_assist.STEP_SHORTLIST, _LLM_PATH, id="atom_relevance"),
    pytest.param(_lead, jev_assist.STEP_LEAD, _LLM_PATH, id="lead_group"),
    # TL-4 has no LLM path to fall back to: without it the grounding gate runs alone.
    pytest.param(_faith, jev_assist.STEP_FAITHFULNESS, _GATE_ALONE, id="faithfulness"),
    pytest.param(_verb, jev_assist.STEP_VERB, _LLM_PATH, id="pick_verb"),
    # Without TL-5 the sweep calls the model for every item, as it did before.
    pytest.param(_sweep, jev_assist.STEP_SWEEP_GATE, _LLM_PATH, id="sweep_flags"),
    # Without TL-7 the run keeps the rephrase's first draft; without TL-8 the letter
    # has its deterministic gate; without TL-9 the report has its literal line.
    pytest.param(_best, jev_assist.STEP_BEST_OF, _LLM_PATH, id="best_variant"),
    pytest.param(_letter, jev_assist.STEP_LETTER, _LLM_PATH, id="letter_unsupported"),
    pytest.param(_meaning, jev_assist.STEP_ATS_MEANING, _LLM_PATH, id="keyword_meaning")])
def test_a_failure_is_named_by_its_class_only(master, helper, step, fallback, caplog):
    caplog.set_level(logging.WARNING, logger=jev_assist.log.name)
    helper(Failing())
    line = jev_assist.usage_line(step)
    assert f"fell back to {fallback} (RuntimeError)" in line
    # The warning was logged, so a silenced logger cannot pass the check below.
    assert f"jev {step} failed (RuntimeError)" in caplog.text
    assert "detail" not in line and "detail" not in caplog.text


class _Unanswered:
    """Answers every question but the last."""

    def judge(self, state, questions):
        answers = jev.FakeJev().judge(state, questions)
        answers.pop(list(questions)[-1])
        return answers


class _NoProbability:
    def judge(self, state, questions):
        return {qid: (jev.Answer(kind="noul", noul=None) if q["type"] == "noul"
                      else jev.Answer(kind="choice", choice="nonsense", confidence=1.0))
                for qid, q in questions.items()}


@pytest.mark.parametrize("judge", [_Unanswered(), _NoProbability()],
                         ids=["unanswered", "malformed"])
@pytest.mark.parametrize("helper", _HELPERS)
def test_a_malformed_answer_returns_none(master, helper, judge):
    assert helper(judge) is None


def test_once_the_breaker_opens_every_later_helper_returns_none(master):
    """JS-3: one outage moves the rest of the run to the LLM path. The first step
    exhausts the retries and opens the breaker; the later steps never reach the
    service and return None at once."""
    down = Down()
    judge = jev.Guarded(down, sleep=lambda _s: None)
    assert jev_assist.skills_pick(_JD, _TITLE, judge=judge) is None
    assert judge.down, "the breaker should be open after the retries"
    tries = down.calls
    assert tries == len(jev.RETRY_DELAYS_S) + 1
    assert jev_assist.atom_relevance(_JD, _TITLE, judge=judge) is None
    assert jev_assist.lead_group(_PROJECTS, judge=judge) is None
    assert jev_assist.faithfulness([_CHESS], judge=judge) is None
    assert jev_assist.pick_verb(_VERB_BULLET, _VERB_OPTIONS, judge=judge) is None
    assert jev_assist.sweep_flags([_plain(_CHESS)], judge=judge) is None
    assert jev_assist.best_variant(_JD, _TITLE, _DRAFTS, judge=judge) is None
    assert jev_assist.letter_unsupported(_SENTENCES, _SOURCES, judge=judge) is None
    assert jev_assist.keyword_meaning(_KEYWORDS, _RESUME, judge=judge) is None
    assert down.calls == tries
    for step in (jev_assist.STEP_SKILLS, jev_assist.STEP_SHORTLIST, jev_assist.STEP_LEAD,
                 jev_assist.STEP_VERB, jev_assist.STEP_SWEEP_GATE, jev_assist.STEP_BEST_OF,
                 jev_assist.STEP_LETTER, jev_assist.STEP_ATS_MEANING):
        assert "fell back to the LLM path (JudgeOutage _Busy 503)" in jev_assist.usage_line(step)
    assert (f"fell back to {_GATE_ALONE} (JudgeOutage _Busy 503)"
            in jev_assist.usage_line(jev_assist.STEP_FAITHFULNESS))


# ── usage ────────────────────────────────────────────────────────────────────
def test_usage_line_names_requests_tokens_and_usd(master):
    rec = Recording(jev.FakeJev())
    jev_assist.atom_relevance(_JD, _TITLE, judge=rec)
    (state, questions), = rec.requests
    tokens = jev.request_size(state, questions)[1]
    assert jev_assist.usage_line(jev_assist.STEP_SHORTLIST) == (
        f"jev shortlist: 1 request, {tokens} tokens (estimated), ${jev.usd_for(tokens):.6f}")
    assert jev_assist.usage(jev_assist.STEP_SHORTLIST) == {
        "requests": 1, "tokens": tokens, "usd": jev.usd_for(tokens), "note": ""}


def test_a_step_that_never_ran_reads_as_zero(master):
    assert jev_assist.usage_line(jev_assist.STEP_LEAD) == (
        "jev lead: 0 requests, 0 tokens (estimated), $0.000000")


def test_nothing_to_ask_is_none_and_says_so(master):
    judge = Failing()
    assert jev_assist.lead_group([], judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_LEAD).endswith("; nothing to ask")


def test_a_step_asked_again_keeps_its_first_failure(master):
    """TL-4 asks after every rewrite, so one run can ask a step many times. The first
    failure stays on its line when a later request goes through: the report has to
    say that a check fell back, whatever came after it."""
    assert _faith(Failing()) is None
    assert _faith(jev.FakeJev()) == {"cc_lead": ""}
    assert _faith(Down()) is None
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["requests"] == 1
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["note"] == (
        f"fell back to {_GATE_ALONE} (RuntimeError)")


def test_nothing_to_ask_stands_only_while_the_step_has_sent_nothing(master):
    step = jev_assist.STEP_FAITHFULNESS
    assert jev_assist.faithfulness([], judge=jev.FakeJev()) is None
    assert jev_assist.usage(step)["note"] == "nothing to ask"
    assert _faith(jev.FakeJev()) == {"cc_lead": ""}
    assert jev_assist.usage(step)["note"] == ""
    assert jev_assist.faithfulness([{"entry": "Empty", "bullets": []}],
                                   judge=jev.FakeJev()) is None
    assert jev_assist.usage(step)["requests"] == 1
    assert jev_assist.usage(step)["note"] == ""


def test_usage_is_counted_per_run_and_per_thread(master):
    jev_assist.atom_relevance(_JD, _TITLE, judge=jev.FakeJev())
    assert jev_assist.usage(jev_assist.STEP_SHORTLIST)["requests"] == 1
    seen = {}

    def other_run():
        seen["before"] = jev_assist.usage(jev_assist.STEP_SHORTLIST)["requests"]
        jev_assist.reset_usage()
        jev_assist.atom_relevance(_JD, _TITLE, judge=jev.FakeJev())
        jev_assist.atom_relevance(_JD, _TITLE, judge=jev.FakeJev())
        seen["after"] = jev_assist.usage(jev_assist.STEP_SHORTLIST)["requests"]

    t = threading.Thread(target=other_run)
    t.start()
    t.join()
    assert seen == {"before": 0, "after": 2}
    assert jev_assist.usage(jev_assist.STEP_SHORTLIST)["requests"] == 1
    jev_assist.reset_usage()
    assert jev_assist.usage(jev_assist.STEP_SHORTLIST)["requests"] == 0


def test_a_request_too_big_to_fit_is_split_and_the_answers_line_up(master, monkeypatch):
    """Batched to fit: past Jev's size limits the atoms go out in several requests,
    each one fitting, and each atom keeps its own answer."""
    probs = dict(zip(_WHATS, (0.9, 0.8, 0.7, 0.6, 0.5, 0.4)))
    whole = Scripted(noul=lambda state, text: probs[_item(state, text, "atoms")])
    want = jev_assist.atom_relevance(_JD, _TITLE, judge=whole)
    (state, questions), = whole.requests
    _longest, total = jev.request_size(state, questions)
    monkeypatch.setattr(jev, "REQUEST_TOKENS_MAX", int(total * 0.8 / jev.SIZE_MARGIN))
    split = Scripted(noul=whole.noul)
    got = jev_assist.atom_relevance(_JD, _TITLE, judge=split)
    assert len(split.requests) > 1
    assert all(jev.request_fits(s, q) for s, q in split.requests)
    assert got == want
    assert [a for s, _q in split.requests for a in s["atoms"]] == _WHATS


# ── TL-1 skills_pick ─────────────────────────────────────────────────────────
def test_skills_pick_is_one_request_a_noul_per_skill_and_the_focus(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.skills_pick(_JD, _TITLE, judge=rec)
    (state, questions), = rec.requests
    assert state["skills"] == _POOL_ORDER
    assert state["job"] == {"title": _TITLE, "description": _JD}
    nouls = [q["instructions"] for q in questions.values() if q["type"] == "noul"]
    assert nouls == [f"Does `job` ask for `skills[{i}]` or a direct equivalent?"
                     for i in range(len(_POOL_ORDER))]
    choices = [q for q in questions.values() if q["type"] == "choice"]
    assert len(choices) == 1
    assert list(choices[0]["criteria"]) == ["ml_research", "backend_platform",
                                            "data_analytics", "general"]
    assert got["skill_focus"] in choices[0]["criteria"]
    assert set(got["probabilities"]) == set(_POOL_ORDER)


def test_skills_pick_orders_each_line_by_probability(master):
    """Most probable first; equal probabilities keep the user's pool order."""
    probs = {"Python": 0.9, "SQL": 0.95, "Go": 0.1, "Flask": 0.2, "Git": 0.5,
             "Docker": 0.5, "Redis": 0.7, "pandas": 0.8}
    judge = Scripted(noul=lambda state, text: probs[_item(state, text, "skills")],
                     choice="data_analytics")
    got = jev_assist.skills_pick(_JD, _TITLE, judge=judge)
    assert got["lines"] == {"Languages": ["SQL", "Python", "Go"], "Frameworks": ["Flask"],
                            "Developer Tools": ["Redis", "Git", "Docker"],
                            "Libraries": ["pandas"]}
    assert got["skill_focus"] == "data_analytics"
    assert got["probabilities"] == probs


def test_skills_pick_asks_a_skill_in_two_pools_once(master, monkeypatch):
    pools = {"Languages": ["Python", "SQL"], "Frameworks": [], "Developer Tools": ["SQL", "Git"],
             "Libraries": []}
    monkeypatch.setattr(jev_assist._skills, "_skill_pools", lambda: pools)
    rec = Recording(jev.FakeJev())
    got = jev_assist.skills_pick(_JD, _TITLE, judge=rec)
    (state, _questions), = rec.requests
    assert state["skills"] == ["Python", "SQL", "Git"]
    assert sorted(got["lines"]["Developer Tools"]) == ["Git", "SQL"]


def test_skills_pick_names_only_the_users_own_skills(master):
    got = jev_assist.skills_pick(_JD, _TITLE, judge=jev.FakeJev())
    pools = jev_assist._skills._skill_pools()
    for label, ranked in got["lines"].items():
        assert sorted(ranked) == sorted(pools[label])


def test_skills_pick_with_no_skills_asks_nothing(master, monkeypatch):
    monkeypatch.setattr(jev_assist._skills, "_skill_pools",
                        lambda: {"Languages": [], "Frameworks": [], "Developer Tools": [],
                                 "Libraries": []})
    judge = Failing()
    assert jev_assist.skills_pick(_JD, _TITLE, judge=judge) is None
    assert judge.calls == 0


def test_the_job_description_is_cut_to_its_cap(master):
    rec = Recording(jev.FakeJev())
    jev_assist.skills_pick("x" * (jev_assist.JD_CHARS + 500), _TITLE, judge=rec)
    (state, _questions), = rec.requests
    assert len(state["job"]["description"]) == jev_assist.JD_CHARS


# ── TL-2 atom_relevance ──────────────────────────────────────────────────────
def test_atom_relevance_rates_every_atom_by_its_what(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.atom_relevance(_JD, _TITLE, judge=rec)
    (state, questions), = rec.requests
    assert state["atoms"] == _WHATS
    assert [q["instructions"] for q in questions.values()] == [
        f"Does `atoms[{i}]` show experience `job` asks for?" for i in range(len(_WHATS))]
    assert all(q["type"] == "noul" for q in questions.values())
    assert list(got) == ["ac_sql", "ac_dash", "or_api", "or_overview", "pa_overview", "cc_lead"]
    assert all(0.0 <= p <= 1.0 for p in got.values())


def test_atom_relevance_maps_each_probability_to_its_atom(master):
    probs = dict(zip(_WHATS, (0.2, 0.9, 0.4, 0.3, 0.8, 0.1)))
    judge = Scripted(noul=lambda state, text: probs[_item(state, text, "atoms")])
    got = jev_assist.atom_relevance(_JD, _TITLE, judge=judge)
    assert got == {"ac_sql": 0.2, "ac_dash": 0.9, "or_api": 0.4, "or_overview": 0.3,
                   "pa_overview": 0.8, "cc_lead": 0.1}


# ── TL-3 lead_group ──────────────────────────────────────────────────────────
def test_lead_group_is_one_choice_per_project_over_its_numbered_bullets(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.lead_group(_PROJECTS, judge=rec)
    (state, questions), = rec.requests
    assert state == {"projects": ["Orbit"]}
    (q,) = questions.values()
    assert q["type"] == "choice"
    assert q["instructions"] == (
        "Which bullet describes `projects[0]` as a whole, saying what the project is?")
    assert q["criteria"] == {"1": _PROJECTS[0]["bullets"][0], "2": _PROJECTS[0]["bullets"][1]}
    # The fake reads words: the bullet that names the project wins, at confidence 1.0.
    assert got == {"Orbit": (2, 1.0)}


def test_lead_group_hands_back_the_confidence(master):
    judge = Scripted(choice="1", confidence=0.4)
    assert jev_assist.lead_group(_PROJECTS, judge=judge) == {"Orbit": (1, 0.4)}


def test_lead_group_asks_every_project_in_one_request(master):
    projects = _PROJECTS + [{"project": "Pantry", "bullets": ["one detail", "Pantry is a planner"]}]
    rec = Recording(jev.FakeJev())
    got = jev_assist.lead_group(projects, judge=rec)
    assert len(rec.requests) == 1
    assert got == {"Orbit": (2, 1.0), "Pantry": (2, 1.0)}


# ── TL-4 faithfulness ────────────────────────────────────────────────────────
def test_faithfulness_is_one_request_per_entry_with_three_questions_a_bullet(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.faithfulness([_ACME, _CHESS], judge=rec)
    assert len(rec.requests) == 2
    (state, questions), (chess_state, _chess_questions) = rec.requests
    assert state == {"entry": "Acme Data",
                     "bullets": [b["text"] for b in _ACME["bullets"]],
                     "atoms": [b["atoms"] for b in _ACME["bullets"]]}
    assert chess_state["entry"] == "Chess Club"
    assert list(questions) == ["supported_0", "inflates_0", "adds_claim_0",
                               "supported_1", "inflates_1", "adds_claim_1"]
    assert questions["supported_1"] == {
        "type": "choice",
        "instructions": "Do `atoms[1]` state every claim `bullets[1]` makes?",
        "criteria": {"verified": "The atoms state every claim the bullet makes.",
                     "unsupported": "The bullet makes a claim the atoms leave out.",
                     "contradicted": "The bullet makes a claim an atom contradicts."}}
    assert questions["inflates_1"] == {"type": "noul", "instructions": (
        'Does `bullets[1]` give the candidate a bigger role, scope or result than '
        '`atoms[1]` state, such as "led" for "helped"?')}
    assert questions["adds_claim_1"] == {"type": "noul", "instructions": (
        "Does `bullets[1]` state a tool, number, outcome or scope that `atoms[1]` "
        "do not state?")}
    # The fake reads each faithful bullet as verified at 1.0 and says no to both nouls.
    assert got == {"ac_sql": "", "ac_dash": "", "cc_lead": ""}


def _reads(choice, confidence, inflates, adds):
    """A judge that picks `choice` at `confidence` and answers the inflation noul
    with `inflates` and the added-claim noul with `adds`."""
    return Scripted(noul=lambda state, text: inflates if "bigger role" in text else adds,
                    choice=choice, confidence=confidence)


_F = jev_assist.FINDINGS


_ALL_THREE = "; ".join([_F["unsupported"], _F["inflates"], _F["adds_claim"]])


@pytest.mark.parametrize("choice,confidence,inflates,adds,want", [
    pytest.param("verified", 0.6, 0.69, 0.69, "", id="verified-at-the-floor-passes"),
    # VL-3: a low-confidence "verified" is the judge reading a faithful rephrase.
    pytest.param("verified", 0.23, 0.1, 0.1, "", id="unsure-verified-passes"),
    pytest.param("unsupported", 0.6, 0.1, 0.1, _F["unsupported"], id="unsupported-at-the-floor"),
    pytest.param("unsupported", 0.59, 0.33, 0.1, "", id="unsure-unsupported-passes"),
    pytest.param("contradicted", 0.6, 0.1, 0.1, _F["contradicted"],
                 id="contradicted-at-the-floor"),
    pytest.param("contradicted", 0.3, 0.1, 0.1, "", id="unsure-contradicted-passes"),
    pytest.param("verified", 1.0, 0.7, 0.1, _F["inflates"], id="inflates-at-the-flag"),
    pytest.param("verified", 0.3, 0.7, 0.1, _F["inflates"], id="inflates-flags-an-unsure-verified"),
    # VL-3: adds_claim reads 0.5 to 0.95 on nearly every faithful rephrase, so it
    # flags nothing alone and rides along in the finding of a bullet flagged otherwise.
    pytest.param("verified", 1.0, 0.1, 0.95, "", id="adds-claim-alone-passes"),
    pytest.param("verified", 1.0, 0.7, 0.7, "; ".join([_F["inflates"], _F["adds_claim"]]),
                 id="adds-claim-named-beside-inflates"),
    pytest.param("unsupported", 0.8, 0.1, 0.69, _F["unsupported"],
                 id="adds-claim-under-the-flag-left-out"),
    pytest.param("unsupported", 0.8, 0.9, 0.9, _ALL_THREE, id="every-finding-in-order"),
])
def test_faithfulness_flags_by_the_thresholds(master, choice, confidence, inflates, adds, want):
    got = jev_assist.faithfulness([_CHESS], judge=_reads(choice, confidence, inflates, adds))
    assert got == {"cc_lead": want}


# VL-3's planted bullets, as the live judge read them: (supported, its confidence,
# inflates, adds_claim) in, the finding out.
@pytest.mark.parametrize("supported,confidence,inflates,adds,want", [
    pytest.param("verified", 0.98, 0.06, 0.17, "", id="faithful-1"),
    pytest.param("verified", 0.85, 0.12, 0.57, "", id="faithful-2"),
    pytest.param("verified", 0.54, 0.33, 0.82, "", id="faithful-3-two-atom-merge"),
    pytest.param("contradicted", 0.73, 0.96, 0.50,
                 "; ".join([_F["contradicted"], _F["inflates"]]), id="inflate-led"),
    pytest.param("unsupported", 0.99, 0.95, 0.97, _ALL_THREE, id="inflate-scope"),
    pytest.param("unsupported", 0.99, 0.83, 0.98, _ALL_THREE, id="adds-number"),
    pytest.param("unsupported", 0.99, 0.59, 0.92,
                 "; ".join([_F["unsupported"], _F["adds_claim"]]), id="adds-tool"),
    pytest.param("unsupported", 0.98, 0.88, 0.97, _ALL_THREE, id="adds-outcome"),
])
def test_the_vl3_planted_bullets_get_their_findings(supported, confidence, inflates, adds, want):
    assert jev_assist._finding(supported, confidence, inflates, adds) == want


def test_the_thresholds_are_the_specs():
    assert jev_assist.SUPPORTED_MIN_CONFIDENCE == 0.6
    assert jev_assist.FAITHFULNESS_FLAG == 0.7


class InflationReader:
    """Reads the planted case the way the live judge is asked to: a bullet that opens
    with "Led" over atoms that say the candidate helped gives a bigger role. Every
    other bullet reads as faithful."""

    def __init__(self):
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        out = {}
        for qid, q in questions.items():
            text = q["instructions"]
            if q["type"] == "choice":
                out[qid] = jev.Answer(kind="choice", choice="verified",
                                      probabilities={n: float(n == "verified")
                                                     for n in q["criteria"]},
                                      confidence=0.9)
                continue
            bullet = _item(state, text, "bullets").lower()
            atoms = str(_item(state, text, "atoms")).lower()
            inflated = ("bigger role" in text and bullet.startswith("led ")
                        and "helped" in atoms)
            out[qid] = jev.Answer(kind="noul", noul=0.95 if inflated else 0.05)
        return out


def test_a_planted_led_the_team_inflation_is_flagged(master):
    """The gap `verify.py` documents: "Led the team" over an atom that says the
    candidate helped carries no distinctive token, so the grounding gate passes it.
    TL-4 names it."""
    planted = {"entry": "Acme Data", "bullets": [
        {"gkey": "ac_sql", "text": "Led the team that wrote SQL reports on warehouse sales data.",
         "atoms": [{"id": "ac_sql",
                    "what": "Helped the team write SQL reports on warehouse sales data"}]},
        _ACME["bullets"][1]]}
    got = jev_assist.faithfulness([planted], judge=InflationReader())
    assert got == {"ac_sql": jev_assist.FINDINGS["inflates"], "ac_dash": ""}
    assert got["ac_sql"] == ("it gives the candidate a bigger role, scope or result "
                             "than its atoms state")


def test_faithfulness_counts_each_request(master):
    rec = Recording(jev.FakeJev())
    jev_assist.faithfulness([_ACME, _CHESS], judge=rec)
    tokens = sum(jev.request_size(s, q)[1] for s, q in rec.requests)
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["requests"] == 2
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["tokens"] == tokens


def test_an_entry_too_big_to_fit_is_split_and_the_findings_line_up(master, monkeypatch):
    judge = _reads("verified", 1.0, 0.1, 0.1)
    whole = jev_assist.faithfulness([_ACME], judge=judge)
    (state, questions), = judge.requests
    _longest, total = jev.request_size(state, questions)
    monkeypatch.setattr(jev, "REQUEST_TOKENS_MAX", int(total * 0.8 / jev.SIZE_MARGIN))
    split = _reads("verified", 1.0, 0.1, 0.1)
    assert jev_assist.faithfulness([_ACME], judge=split) == whole
    assert len(split.requests) == 2
    assert [s["bullets"] for s, _q in split.requests] == [[b["text"]] for b in _ACME["bullets"]]
    assert all(s["entry"] == "Acme Data" for s, _q in split.requests)


def test_faithfulness_with_no_bullets_asks_nothing(master):
    judge = Failing()
    assert jev_assist.faithfulness([{"entry": "Empty", "bullets": []}], judge=judge) is None
    assert judge.calls == 0


# ── TL-6 pick_verb ───────────────────────────────────────────────────────────
def test_pick_verb_is_one_choice_over_the_options(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.pick_verb(_VERB_BULLET, _VERB_OPTIONS, judge=rec)
    (state, questions), = rec.requests
    assert state == {"bullet": _VERB_BULLET}
    assert questions == {"verb": {
        "type": "choice", "instructions": "Which verb best names the action in `bullet`?",
        "criteria": _VERB_OPTIONS}}
    # The fake reads words: "analyze" in the bullet is the Analyze category's word.
    assert got == ("Modeled", 1.0)


def test_pick_verb_hands_back_the_confidence(master):
    judge = Scripted(choice="Coordinated", confidence=0.4)
    assert jev_assist.pick_verb(_VERB_BULLET, _VERB_OPTIONS, judge=judge) == ("Coordinated", 0.4)


def test_pick_verb_with_no_option_asks_nothing(master):
    judge = Failing()
    assert jev_assist.pick_verb(_VERB_BULLET, {}, judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_VERB).endswith("; nothing to ask")


def test_a_palette_too_big_to_fit_is_cut_from_its_end(master, monkeypatch):
    """One choice cannot be split, so past Jev's limits the options are halved from
    the end, which keeps the repeated verb's own category (it comes first)."""
    options = {f"Verb{i:03d}": "Build" for i in range(64)}
    whole = Recording(jev.FakeJev())
    jev_assist.pick_verb(_VERB_BULLET, options, judge=whole)
    (state, questions), = whole.requests
    longest, _total = jev.request_size(state, questions)
    monkeypatch.setattr(jev, "STATE_TOKENS_MAX", int(longest * 0.7 / jev.SIZE_MARGIN))
    cut = Recording(jev.FakeJev())
    verb, _conf = jev_assist.pick_verb(_VERB_BULLET, options, judge=cut)
    (state, questions), = cut.requests
    criteria = questions["verb"]["criteria"]
    assert jev.request_fits(state, questions)
    assert list(criteria) == list(options)[:len(criteria)] and 1 <= len(criteria) < 64
    assert verb in criteria


def test_the_verb_floor_is_the_specs():
    assert jev_assist.VERB_MIN_CONFIDENCE == 0.5


# ── TL-5 sweep_flags ─────────────────────────────────────────────────────────
_TELLS = ["contrast framing", "stacked adjectives", "filler or vague impact", "hype words",
          "padded list of three"]


def test_sweep_flags_is_one_request_per_entry_with_five_nouls_a_bullet(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.sweep_flags([_plain(_ACME), _plain(_CHESS)], judge=rec)
    assert len(rec.requests) == 2
    (state, questions), (chess_state, _chess_questions) = rec.requests
    assert state == {"entry": "Acme Data", "bullets": [b["text"] for b in _ACME["bullets"]]}
    assert chess_state["entry"] == "Chess Club"
    assert list(questions) == [f"{qid}_{i}" for i in range(2)
                               for qid in ("contrast", "stacked", "filler", "hype", "three")]
    assert questions["contrast_1"] == {"type": "noul", "instructions": (
        "Does `bullets[1]` use contrast framing, which defines a thing by what it is not?")}
    assert questions["stacked_1"]["instructions"] == (
        "Does `bullets[1]` stack adjectives in front of a noun?")
    assert questions["filler_1"]["instructions"] == (
        "Does `bullets[1]` hold filler words or a vague claim of impact?")
    assert questions["hype_1"]["instructions"] == (
        "Does `bullets[1]` use hype words or self-praise?")
    assert questions["three_1"]["instructions"] == (
        "Does `bullets[1]` pad a list out to three items?")
    # The fake finds none of the questions' words in these bullets.
    assert got == {"ac_sql": (), "ac_dash": (), "cc_lead": ()}


def test_the_fake_reads_a_tell_by_its_words(master):
    entry = {"entry": "Chess Club", "bullets": [
        {"gkey": "cc_lead", "text": "Wrote words of self praise for the club site."}]}
    assert jev_assist.sweep_flags([entry], judge=jev.FakeJev()) == {"cc_lead": ("hype words",)}


@pytest.mark.parametrize("p,want", [pytest.param(0.6, ("hype words",), id="at-the-flag"),
                                    pytest.param(0.59, (), id="under-the-flag")])
def test_sweep_flags_flags_at_the_threshold(master, p, want):
    judge = Scripted(noul=lambda state, text: p if "hype" in text else 0.1)
    assert jev_assist.sweep_flags([_plain(_CHESS)], judge=judge) == {"cc_lead": want}


def test_sweep_flags_names_every_tell_in_order(master):
    judge = Scripted(noul=lambda state, text: 0.9)
    assert jev_assist.sweep_flags([_plain(_CHESS)], judge=judge) == {"cc_lead": tuple(_TELLS)}


def test_the_sweep_gate_is_the_specs():
    assert jev_assist.SWEEP_FLAG == 0.6
    assert list(jev_assist.SWEEP_QUESTIONS) == _TELLS


def test_sweep_flags_counts_each_request(master):
    rec = Recording(jev.FakeJev())
    jev_assist.sweep_flags([_plain(_ACME), _plain(_CHESS)], judge=rec)
    tokens = sum(jev.request_size(s, q)[1] for s, q in rec.requests)
    assert jev_assist.usage(jev_assist.STEP_SWEEP_GATE)["requests"] == 2
    assert jev_assist.usage(jev_assist.STEP_SWEEP_GATE)["tokens"] == tokens


def test_an_entry_too_big_to_fit_is_split_and_the_flags_line_up(master, monkeypatch):
    def noul(state, text):
        return 0.9 if "hype" in text and "dashboard" in _item(state, text, "bullets") else 0.1

    whole = Scripted(noul=noul)
    want = jev_assist.sweep_flags([_plain(_ACME)], judge=whole)
    assert want == {"ac_sql": (), "ac_dash": ("hype words",)}
    (state, questions), = whole.requests
    _longest, total = jev.request_size(state, questions)
    monkeypatch.setattr(jev, "REQUEST_TOKENS_MAX", int(total * 0.8 / jev.SIZE_MARGIN))
    split = Scripted(noul=noul)
    assert jev_assist.sweep_flags([_plain(_ACME)], judge=split) == want
    assert [s["bullets"] for s, _q in split.requests] == [[b["text"]] for b in _ACME["bullets"]]


def test_sweep_flags_with_no_bullets_asks_nothing(master):
    judge = Failing()
    assert jev_assist.sweep_flags([{"entry": "Empty", "bullets": []}], judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_SWEEP_GATE).endswith("; nothing to ask")


# ── TL-7 best_variant ────────────────────────────────────────────────────────
def test_best_variant_is_one_choice_per_bullet_over_its_numbered_drafts(master):
    rec = Recording(jev.FakeJev())
    got = jev_assist.best_variant(_JD, _TITLE, _DRAFTS, judge=rec)
    (state, questions), = rec.requests
    assert state == {"job": {"title": _TITLE, "description": _JD}}
    assert questions == {"bullet_0": {
        "type": "choice",
        "instructions": "Which draft shows the most of what `job` asks for?",
        "criteria": {"draft 1": _DRAFTS[0]["drafts"][0],
                     "draft 2": _DRAFTS[0]["drafts"][1]}}}
    # The fake reads words: draft 2 shares "weekly", "sales" and "leaders" with the job.
    assert got == {"ac_sql": (2, 1.0)}


def test_best_variant_hands_back_the_confidence(master):
    judge = Scripted(choice="draft 1", confidence=0.3)
    assert jev_assist.best_variant(_JD, _TITLE, _DRAFTS, judge=judge) == {"ac_sql": (1, 0.3)}


def test_best_variant_leaves_out_a_bullet_with_one_draft(master):
    rec = Recording(jev.FakeJev())
    groups = _DRAFTS + [{"gkey": "cc_lead", "drafts": ["Led weekly training sessions."]}]
    got = jev_assist.best_variant(_JD, _TITLE, groups, judge=rec)
    assert list(got) == ["ac_sql"]
    (_state, questions), = rec.requests
    assert list(questions) == ["bullet_0"]


def test_best_variant_with_nothing_to_choose_asks_nothing(master):
    judge = Failing()
    one = [{"gkey": "ac_sql", "drafts": ["Wrote SQL reports."]}]
    assert jev_assist.best_variant(_JD, _TITLE, one, judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_BEST_OF).endswith("; nothing to ask")


def test_many_bullets_too_big_to_fit_are_split_and_the_picks_line_up(master, monkeypatch):
    groups = [{"gkey": f"g{k}", "drafts": [f"Wrote report {k} in SQL.",
                                           f"Wrote weekly sales report {k} in SQL."]}
              for k in range(40)]
    whole = Recording(jev.FakeJev())
    want = jev_assist.best_variant(_JD, _TITLE, groups, judge=whole)
    (state, questions), = whole.requests
    _longest, total = jev.request_size(state, questions)
    monkeypatch.setattr(jev, "REQUEST_TOKENS_MAX", int(total * 0.6 / jev.SIZE_MARGIN))
    split = Recording(jev.FakeJev())
    got = jev_assist.best_variant(_JD, _TITLE, groups, judge=split)
    assert len(split.requests) > 1
    assert all(jev.request_fits(s, q) for s, q in split.requests)
    assert got == want


# ── TL-8 letter_unsupported ──────────────────────────────────────────────────
def test_letter_unsupported_is_one_noul_per_sentence_against_the_sources(master):
    rec = Recording(jev.FakeJev())
    jev_assist.letter_unsupported(_SENTENCES, _SOURCES, judge=rec)
    (state, questions), = rec.requests
    assert state == {"sentences": _SENTENCES, "sources": _SOURCES}
    assert questions == {f"claims_{i}": {
        "type": "noul",
        "instructions": f"Does `sentences[{i}]` claim something about the candidate that "
                        "`sources` do not state?"} for i in range(2)}


def test_letter_unsupported_never_carries_the_job(master):
    rec = Recording(jev.FakeJev())
    jev_assist.letter_unsupported(_SENTENCES, _SOURCES, judge=rec)
    (state, _questions), = rec.requests
    assert "job" not in state and _JD not in repr(state)


@pytest.mark.parametrize("p, flagged", [(0.69, False), (0.7, True)])
def test_letter_unsupported_flags_at_the_threshold(master, p, flagged):
    def noul(state, text):
        return p if "sentences[1]" in text else 0.0
    got = jev_assist.letter_unsupported(_SENTENCES, _SOURCES, judge=Scripted(noul=noul))
    assert got == ([_SENTENCES[1]] if flagged else [])


def test_letter_unsupported_with_no_sentence_asks_nothing(master):
    judge = Failing()
    assert jev_assist.letter_unsupported(["", "  "], _SOURCES, judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_LETTER).endswith("; nothing to ask")


def test_the_letter_threshold_is_the_reports():
    assert jev_assist.LETTER_CLAIM_FLAG == 0.7


# ── TL-9 keyword_meaning ─────────────────────────────────────────────────────
def test_keyword_meaning_is_one_noul_per_keyword_against_the_resume(master):
    rec = Recording(jev.FakeJev())
    jev_assist.keyword_meaning(_KEYWORDS, _RESUME, judge=rec)
    (state, questions), = rec.requests
    assert state == {"resume": _RESUME, "keywords": _KEYWORDS}
    assert questions == {f"keywords_{i}": {
        "type": "noul",
        "instructions": f"Does `resume` show `keywords[{i}]` or a direct equivalent?"}
        for i in range(3)}


@pytest.mark.parametrize("p, shown", [(0.49, False), (0.5, True)])
def test_keyword_meaning_counts_at_the_threshold(master, p, shown):
    def noul(state, text):
        return p if "keywords[2]" in text else 1.0
    got = jev_assist.keyword_meaning(_KEYWORDS, _RESUME, judge=Scripted(noul=noul))
    assert got == (_KEYWORDS if shown else _KEYWORDS[:2])


def test_keyword_meaning_cuts_the_resume_to_its_cap(master):
    rec = Recording(jev.FakeJev())
    jev_assist.keyword_meaning(_KEYWORDS, "x" * (jev_assist.JD_CHARS + 50), judge=rec)
    (state, _questions), = rec.requests
    assert len(state["resume"]) == jev_assist.JD_CHARS


def test_keyword_meaning_with_no_keyword_asks_nothing(master):
    judge = Failing()
    assert jev_assist.keyword_meaning([], _RESUME, judge=judge) is None
    assert judge.calls == 0
    assert jev_assist.usage_line(jev_assist.STEP_ATS_MEANING).endswith("; nothing to ask")


def test_the_meaning_threshold_is_the_reports():
    assert jev_assist.KEYWORD_MEANING_MIN == 0.5


# ── the wording ──────────────────────────────────────────────────────────────
def test_every_judge_question_is_free_of_the_banned_phrasing():
    """The questions follow the rules the prompts do: `compose.style_violations`
    finds none of its `_STYLE_BANS` shapes in them, and they carry no em dash
    (U+2014) and no clause that opens with a comma and "never". TL-4's findings
    count too: they ride in the reground prompt."""
    texts = [jev_assist.SKILL_QUESTION, jev_assist.ATOM_QUESTION, jev_assist.LEAD_QUESTION,
             jev_assist.FOCUS_QUESTION, *jev_assist.SKILL_FOCUS.values(),
             jev_assist.SUPPORTED_QUESTION, *jev_assist.SUPPORTED_OPTIONS.values(),
             jev_assist.INFLATES_QUESTION, jev_assist.ADDS_CLAIM_QUESTION,
             *jev_assist.FINDINGS.values(), jev_assist.PICK_VERB_QUESTION,
             *jev_assist.SWEEP_QUESTIONS.values(), jev_assist.BEST_DRAFT_QUESTION,
             jev_assist.LETTER_CLAIM_QUESTION, jev_assist.KEYWORD_MEANING_QUESTION]
    for text in texts:
        assert compose.style_violations(text) == [], text
        assert "\u2014" not in text and not re.search(r",\s*never\s", text), text
