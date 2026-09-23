"""The SP8 record / replay harness for the runner tests: `jev_harness` (the
judge selector, the cost guard), `jev_outcomes` (the jsonl writer),
`conftest_jev` (the `jev_judge` fixture and its hooks) and
`scripts/jev_thresholds.py` (the tuning printout).

Hermetic: no key, no network. The "live" judge in every test here is a fake
that bumps the process usage counter; the caches are built from `FakeJev`
answers in `tmp_path`. The fixture and hook tests run an inner pytest through
`pytester` in-process, with the plugin loaded exactly as the runner test
modules load it."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
import jev_harness  # noqa: E402
import jev_outcomes  # noqa: E402

pytest_plugins = ["pytester", "conftest_jev"]

STATE = {"page": {"title": "Apply for the analytics engineer role"},
         "fields": [{"n": 0, "label": "Email"}]}
QUESTIONS = {
    "page_state": {"type": "choice", "instructions": "What kind of page is `page`?",
                   "criteria": {"application_form": "a form to apply for a role",
                                "other": "anything else"}},
    "verify_0": {"type": "noul",
                 "instructions": "The email field holds the analytics engineer email"},
}


class _Bumping:
    """A stand-in for the live judge: answers like the fake and adds
    `tokens` input tokens to the process usage counter per call."""

    def __init__(self, tokens=1_000_000):
        self.tokens = tokens
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        jev._USAGE["requests"] += 1
        jev._USAGE["input_tokens"] += self.tokens
        return jev.FakeJev().judge(state, questions)


@pytest.fixture(autouse=True)
def _clean_usage():
    jev.reset_usage()
    yield
    jev.reset_usage()


# --- the selector ---------------------------------------------------------------------

def test_mode_defaults_to_fake_and_rejects_an_unknown_value():
    assert jev_harness.mode_from({}) == "fake"
    assert jev_harness.mode_from({jev_harness.MODE_ENV: " Replay "}) == "replay"
    with pytest.raises(ValueError, match="AUTO_APPLY_TEST_JEV"):
        jev_harness.mode_from({jev_harness.MODE_ENV: "live"})


def test_cache_path_comes_from_the_env_else_the_tracked_fixture_file(tmp_path):
    assert jev_harness.cache_path_from({}) == REPO / jev.DEFAULT_CACHE
    assert jev_harness.cache_path_from({jev.CACHE_ENV: str(tmp_path / "c.json")}) == tmp_path / "c.json"


def test_cap_comes_from_the_env_and_defaults_to_one_dollar():
    assert jev_harness.cap_from({}) == 1.0
    assert jev_harness.cap_from({jev_harness.CAP_ENV: "0.25"}) == 0.25


def test_fake_mode_hands_out_a_fresh_fake_and_never_opens_the_cache(tmp_path):
    s = jev_harness.Session("fake", tmp_path / "cache.json")
    assert isinstance(s.judge(), jev.FakeJev)
    assert s.skip_reason() is None
    assert not (tmp_path / "cache.json").exists() and not s.soft


def test_replay_mode_wraps_a_read_only_replay_over_the_cache(tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(jev.FakeJev(), cache).judge(STATE, QUESTIONS)
    s = jev_harness.Session("replay", cache)
    s.begin("tests/test_x.py::test_a")
    judge = s.judge()
    assert isinstance(judge, jev_harness.Observed)
    assert isinstance(judge.inner, jev.ReplayJev) and judge.inner.inner is None
    answers = judge.judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"
    assert jev.usage()["requests"] == 0


def test_record_mode_records_through_the_live_factory(tmp_path, monkeypatch):
    live = _Bumping()
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    cache = tmp_path / "cache.json"
    s = jev_harness.Session("record", cache, env={jev.KEY_ENV: "k-test"})
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    assert live.calls == 1 and cache.is_file()
    s.judge().judge(STATE, QUESTIONS)
    assert live.calls == 1, "the second request replays from the cache"


def test_record_mode_without_a_key_skips_with_the_reason(tmp_path):
    s = jev_harness.Session("record", tmp_path / "cache.json", env={})
    reason = s.skip_reason()
    assert reason and jev.KEY_ENV in reason and "record" in reason


def test_judge_outside_a_test_is_fake_only_in_fake_mode(monkeypatch):
    jev_harness.deactivate()
    monkeypatch.delenv(jev_harness.MODE_ENV, raising=False)
    assert isinstance(jev_harness.judge(), jev.FakeJev)
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    with pytest.raises(RuntimeError, match="jev_judge"):
        jev_harness.judge()


# --- the observer, misses and the outcome record --------------------------------------

def test_replay_miss_is_recorded_on_the_test_and_re_raised(tmp_path):
    s = jev_harness.Session("replay", tmp_path / "cache.json")
    rec = s.begin("tests/test_apply_run.py::test_a")
    with pytest.raises(jev.JevUnavailable):
        s.judge().judge(STATE, QUESTIONS)
    assert len(rec.misses) == 1
    assert rec.misses[0]["questions"] == ["page_state", "verify_0"]
    text = s.miss_text(rec)
    assert "jev_judge" in text and "tests/test_apply_run.py::test_a" in text
    assert "AUTO_APPLY_TEST_JEV=record" in text and str(tmp_path / "cache.json") in text


class _Raising:
    """A stand-in for a live judge whose SDK / HTTP layer blows up with a
    message that carries a secret-looking token."""

    def judge(self, state, questions):
        raise RuntimeError("401 from https://api.typesafe.ai: bad key sk-live-SECRET-9f3a")


def test_observed_records_only_the_exception_type_and_re_raises(tmp_path, monkeypatch):
    """An SDK / HTTP error in record mode lands in `outcomes.jsonl` as the type
    name alone: the message can quote the request, the URL or the key."""
    monkeypatch.setattr(jev_harness, "live_judge", lambda: _Raising())
    s = jev_harness.Session("record", tmp_path / "cache.json", env={jev.KEY_ENV: "k-test"})
    rec = s.begin("t::boom")
    with pytest.raises(RuntimeError):
        s.judge().judge(STATE, QUESTIONS)
    assert rec.answers == [{"error": "RuntimeError"}]
    assert rec.misses == []
    writer = jev_outcomes.OutcomesWriter(jev_harness.outcomes_path(tmp_path / "cache.json"))
    writer.reset()
    writer.write(rec)
    text = writer.path.read_text(encoding="utf-8")
    assert "RuntimeError" in text
    assert "SECRET" not in text and "typesafe.ai" not in text


def test_observed_answers_carry_the_fake_answer_beside_the_recorded_one(tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(jev.FakeJev(), cache).judge(STATE, QUESTIONS)
    s = jev_harness.Session("replay", cache)
    rec = s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    rows = {r["qid"]: r for r in rec.answers}
    assert rows["page_state"]["kind"] == "choice"
    assert rows["page_state"]["choice"] == "application_form"
    assert rows["page_state"]["fake"]["choice"] == "application_form"
    assert rows["verify_0"]["kind"] == "noul" and rows["verify_0"]["noul"] == 0.9


# --- the cost guard --------------------------------------------------------------------

def test_cap_guard_stops_the_session_once_the_spend_passes_the_cap(tmp_path, monkeypatch):
    live = _Bumping(tokens=30_000_000)     # 1.26 USD per call at 0.042 USD / Mtok
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0,
                            env={jev.KEY_ENV: "k-test"})
    assert s.skip_reason() is None
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    s.end("t::a")
    assert s.spent_usd == pytest.approx(1.26) and s.requests == 1
    reason = s.skip_reason()
    assert reason and jev_harness.CAP_ENV in reason and "1.26" in reason
    assert s.stopped == reason


def test_cap_guard_counts_only_the_spend_inside_tests(tmp_path, monkeypatch):
    live = _Bumping(tokens=30_000_000)
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0,
                            env={jev.KEY_ENV: "k-test"})
    jev._USAGE["input_tokens"] += 50_000_000     # spent by something else first
    s.begin("t::a")
    s.end("t::a")
    assert s.spent_usd == 0.0 and s.skip_reason() is None


# --- the outcomes writer ----------------------------------------------------------------

def test_outcomes_writer_appends_one_json_line_per_record(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    w = jev_outcomes.OutcomesWriter(path)
    w.reset()
    assert path.read_text(encoding="utf-8") == ""
    a = jev_outcomes.TestRecord(test="t::a", mode="replay")
    a.outcomes.append({"job_id": "42", "status": "submitted", "reason": "confirmation"})
    a.result = "passed"
    b = jev_outcomes.TestRecord(test="t::b", mode="replay", divergence="assert x", result="xfail")
    w.write(a)
    w.write(b)
    rows = jev_outcomes.read_outcomes(path)
    assert [r["test"] for r in rows] == ["t::a", "t::b"]
    assert rows[0]["outcomes"][0]["status"] == "submitted"
    assert rows[1]["divergence"] == "assert x" and rows[1]["result"] == "xfail"
    for line in path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_outcomes_path_sits_beside_the_cache(tmp_path):
    assert jev_harness.outcomes_path(tmp_path / "x" / "cache.json") == tmp_path / "x" / "outcomes.jsonl"


# --- the fixture and hooks, through an inner pytest ------------------------------------

_INNER = '''
import jev, jev_harness
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    answers = jev_judge().judge(STATE, QUESTIONS)
    assert answers["page_state"].choice == "application_form"

def test_two(jev_judge):
    answers = jev_harness.judge().judge(STATE, QUESTIONS)
    assert answers["verify_0"].noul == {expected_noul!r}

def test_three(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)
    raise KeyError("not an assertion")
'''


def _inner(pytester, expected_noul=0.9):
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER.format(STATE=STATE, QUESTIONS=QUESTIONS,
                                                 expected_noul=expected_noul))


def test_fixture_in_fake_mode_passes_and_writes_nothing(pytester, monkeypatch, tmp_path):
    monkeypatch.delenv(jev_harness.MODE_ENV, raising=False)
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=1)
    assert not (tmp_path / "cache.json").exists()
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_fixture_in_replay_mode_fails_a_miss_naming_the_fixture_and_test(
        pytester, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=3)
    out = result.stdout.str()
    assert "jev_judge" in out and "test_inner.py::test_one" in out
    assert "AUTO_APPLY_TEST_JEV=record" in out


def test_fixture_in_replay_mode_replays_and_turns_a_divergence_into_an_xfail(
        pytester, monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(jev.FakeJev(), cache).judge(STATE, QUESTIONS)
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    _inner(pytester, expected_noul=0.1)       # test_two's assertion diverges
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, xfailed=1, failed=1)
    rows = {r["test"]: r for r in jev_outcomes.read_outcomes(tmp_path / "outcomes.jsonl")}
    assert set(rows) == {"test_inner.py::test_one", "test_inner.py::test_two",
                         "test_inner.py::test_three"}
    two = rows["test_inner.py::test_two"]
    assert two["result"] == "xfail" and "0.1" in two["divergence"]
    assert two["answers"][0]["qid"] == "page_state"
    assert rows["test_inner.py::test_one"]["result"] == "passed"
    assert rows["test_inner.py::test_three"]["result"] == "failed"
    assert "jev replay" in result.stdout.str()


def test_fixture_in_record_mode_without_a_key_skips_every_test(pytester, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=3)
    assert jev.KEY_ENV in result.stdout.str()
    assert not (tmp_path / "cache.json").exists()


def test_fixture_in_record_mode_stops_at_the_cap_and_skips_the_rest(
        pytester, monkeypatch, tmp_path):
    live = _Bumping(tokens=30_000_000)
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev.KEY_ENV, "k-test")
    monkeypatch.setenv(jev_harness.CAP_ENV, "1.00")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, skipped=2)
    out = result.stdout.str()
    assert live.calls == 1 and jev_harness.CAP_ENV in out
    assert "jev record:" in out and "cap 1.00 USD" in out
    assert (tmp_path / "cache.json").is_file()


# --- the thresholds helper ------------------------------------------------------------------

def _tuning_cache(tmp_path):
    """A cache with one answer of every question kind, from the fake."""
    cache = tmp_path / "cache.json"
    r = jev.ReplayJev(jev.FakeJev(), cache)
    page_qs = {
        "page_state": QUESTIONS["page_state"],
        "field_0_source": {"type": "choice", "instructions": "Which fact fills `fields[0]`?",
                           "criteria": {"email": "the email address", "leave_blank": "nothing"}},
        "field_1_source": {"type": "choice", "instructions": "Which fact fills the consent box?",
                           "criteria": {"consent_attest": "consent box", "leave_blank": "nothing"}},
        "field_2_option": {"type": "choice", "instructions": "Which option is United States?",
                           "criteria": {"United States": "the USA", "no_match": "nothing"}},
        "button_0_role": {"type": "choice", "instructions": "The button says Submit application",
                          "criteria": {"advance": "next step", "submit": "submit application"}},
        "button_1_role": {"type": "choice", "instructions": "The button says Next step",
                          "criteria": {"advance": "next step", "submit": "submit application"}},
        "asks_for_prohibited": {"type": "noul", "instructions": "asks for a social security number"},
        "requires_account": {"type": "noul", "instructions": "requires an account"},
        "has_captcha": {"type": "noul", "instructions": "shows a captcha"},
    }
    r.judge(STATE, page_qs)
    r.judge({"filled": [{"n": 0, "value": "jane@example.com"}], "sheet_excerpt": "email jane"},
            {"verify_0": {"type": "noul", "instructions": "the email value is correct"},
             "placeholder_0": {"type": "noul", "instructions": "the value looks like a placeholder"}})
    r.judge({"messages": [{"subject": "Your verification code"}]},
            {"msg_0_from_site": {"type": "noul", "instructions": "from the site"},
             "msg_0_has_code": {"type": "noul", "instructions": "carries a verification code"},
             "code_pick": {"type": "choice", "instructions": "the code in the message",
                           "criteria": {"ABC123": "candidate", "none": "no code"}}})
    r.judge({"sentence": "I built the pipeline", "sheet": "Built the ingestion pipeline"},
            {"grounded_0": {"type": "noul", "instructions": "the sentence is supported by `sheet`"}})
    return cache


def test_thresholds_helper_prints_every_kind_with_its_threshold(tmp_path):
    cache = _tuning_cache(tmp_path)
    w = jev_outcomes.OutcomesWriter(jev_harness.outcomes_path(cache))
    w.reset()
    rec = jev_outcomes.TestRecord(test="tests/test_apply_run.py::test_a", mode="replay",
                                  divergence="AssertionError: needs_human", result="xfail")
    rec.outcomes.append({"job_id": "42", "status": "needs_human", "reason": "page state"})
    w.write(rec)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "jev_thresholds.py"),
                           "--cache", str(cache)], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for kind in ("page state", "field map", "consent", "option", "button submit",
                 "button advance", "verify", "placeholder", "prohibited", "captcha",
                 "inbox", "grounding", "code pick"):
        assert kind in out, kind
    assert "PAGE_STATE_MIN_CONF" in out and "0.40" in out
    assert "BUTTON_SUBMIT_MIN_CONF" in out and "GROUNDING_MIN" in out
    assert "thresholds tuned" in out and "UNTUNED" not in out
    assert "test_a" in out and "needs_human" in out and "xfail" in out


def test_thresholds_helper_handles_an_empty_cache(tmp_path):
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "jev_thresholds.py"),
                           "--cache", str(tmp_path / "missing.json")],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "no answers recorded" in proc.stdout and "page state" in proc.stdout
