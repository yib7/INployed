"""jev_assist: the tailor's Jev requests (TL-1 to TL-3).

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


_HELPERS = [pytest.param(_skills, id="skills_pick"), pytest.param(_atoms, id="atom_relevance"),
            pytest.param(_lead, id="lead_group")]


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
    assert areas == ["tailor", "tailor", "tailor"]


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
    def client(area):
        raise AssertionError("judge=None must not build a client")

    monkeypatch.setattr(jev_switch, "client", client)
    assert helper(None) is None


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


@pytest.mark.parametrize("helper,step", [
    pytest.param(_skills, jev_assist.STEP_SKILLS, id="skills_pick"),
    pytest.param(_atoms, jev_assist.STEP_SHORTLIST, id="atom_relevance"),
    pytest.param(_lead, jev_assist.STEP_LEAD, id="lead_group")])
def test_a_failure_is_named_by_its_class_only(master, helper, step, caplog):
    helper(Failing())
    line = jev_assist.usage_line(step)
    assert "fell back to the LLM path (RuntimeError)" in line
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
    assert down.calls == tries
    for step in (jev_assist.STEP_SKILLS, jev_assist.STEP_SHORTLIST, jev_assist.STEP_LEAD):
        assert "fell back to the LLM path (JudgeOutage _Busy 503)" in jev_assist.usage_line(step)


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


# ── the wording ──────────────────────────────────────────────────────────────
def test_every_judge_question_is_free_of_the_banned_phrasing():
    """The questions follow the rules the prompts do: `compose.style_violations`
    finds none of its `_STYLE_BANS` shapes in them, and they carry no em dash
    (U+2014) and no clause that opens with a comma and "never"."""
    texts = [jev_assist.SKILL_QUESTION, jev_assist.ATOM_QUESTION, jev_assist.LEAD_QUESTION,
             jev_assist.FOCUS_QUESTION, *jev_assist.SKILL_FOCUS.values()]
    for text in texts:
        assert compose.style_violations(text) == [], text
        assert "\u2014" not in text and not re.search(r",\s*never\s", text), text
