"""The record / replay harness for the runner tests: `jev_harness` (the
judge selector, the cost guard), `jev_outcomes` (the jsonl writer),
`conftest_jev` (the `jev_judge` fixture and its hooks) and
`scripts/jev_thresholds.py` (the tuning printout).

Hermetic: no key, no network. The "live" judge in every test here is a fake
that bumps the process usage counter; the caches are built from `FakeJev`
answers in `tmp_path`. The fixture and hook tests run an inner pytest through
`pytester` in-process, with the plugin loaded exactly as the runner test
modules load it."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
import jev_doubles  # noqa: E402
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
    """A stand-in for the live judge: answers like the fake and counts one
    live request of `tokens` input tokens per call (`jev.count_usage`, so
    both the resettable counter and the lifetime one see it)."""

    def __init__(self, tokens=1_000_000):
        self.tokens = tokens
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        jev.count_usage(self.tokens)
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


def test_cap_comes_from_the_env_and_a_live_recording_without_it_is_refused(tmp_path):
    # a live recording names its cap; only a dry run has a default
    assert jev_harness.cap_from({jev_harness.CAP_ENV: "0.25"}) == 0.25
    with pytest.raises(ValueError, match=jev_harness.CAP_ENV):
        jev_harness.cap_from({})
    assert jev_harness.cap_from({}, live=False) == jev_harness.DRY_CAP_USD == 0.88
    key = {jev.KEY_ENV: "k-test", jev_harness.MODE_ENV: "record"}
    with pytest.raises(ValueError, match="jev_record.ps1 -Cap"):
        jev_harness.Session("record", tmp_path / "cache.json", env=key)
    with pytest.raises(ValueError, match=jev_harness.CAP_ENV):
        jev_harness.Session.from_env(key)
    assert not (tmp_path / "cache.json").exists()
    dry = jev_harness.Session.from_env({**key, jev_harness.DRY_ENV: "1"})
    assert dry.dry and dry.cap_usd == 0.88
    assert jev_harness.Session.from_env({**key, jev_harness.CAP_ENV: "0.3"}).cap_usd == 0.3
    assert jev_harness.Session("replay", tmp_path / "cache.json", env={}).cap_usd == 0.88


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
    s = jev_harness.Session("record", cache, cap_usd=1.0, env={jev.KEY_ENV: "k-test"})
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    assert live.calls == 1 and cache.is_file()
    s.judge().judge(STATE, QUESTIONS)
    assert live.calls == 1, "the second request replays from the cache"


def test_record_mode_without_a_key_skips_with_the_reason(tmp_path):
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0, env={})
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
    # the re-record command is the script, which names the cap
    assert r"jev_record.ps1 -Target runner -Cap <USD>" in text
    assert str(tmp_path / "cache.json") in text


class _Raising:
    """A stand-in for a live judge whose SDK / HTTP layer blows up with a
    message that carries a secret-looking token."""

    def judge(self, state, questions):
        raise RuntimeError("401 from https://api.typesafe.ai: bad key sk-live-SECRET-9f3a")


def test_observed_records_only_the_exception_type_and_re_raises(tmp_path, monkeypatch):
    """An SDK / HTTP error in record mode lands in `outcomes.jsonl` as the type
    name alone: the message can quote the request, the URL or the key."""
    monkeypatch.setattr(jev_harness, "live_judge", lambda: _Raising())
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0,
                            env={jev.KEY_ENV: "k-test"})
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
    jev.count_usage(50_000_000)                  # spent by something else first
    s.begin("t::a")
    s.end("t::a")
    assert s.spent_usd == 0.0 and s.skip_reason() is None


# --- the per-request cap and the dry run -------------------------------------------------

class _Sized:
    """A stand-in for the live judge that counts each request at its
    estimated size, as `TypeSafeJev` counts the service's own count."""

    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        jev.count_usage(jev.request_size(state, questions)[1])
        return jev.FakeJev().judge(state, questions)


def _one_request_usd():
    return jev.usd_for(jev.request_size(STATE, QUESTIONS)[1])


def test_spend_cap_refuses_the_request_that_would_pass_it_and_every_one_after():
    live = _Sized()
    cap = jev.SpendCap(live, cap_usd=_one_request_usd() * 1.5)
    cap.judge(STATE, QUESTIONS)
    assert live.calls == 1 and not cap.reached
    with pytest.raises(jev.SpendCapReached) as e:
        cap.judge(STATE, QUESTIONS)     # the spend so far plus this one's estimate passes it
    assert live.calls == 1, "a refused request never leaves"
    assert cap.reached and cap.refused == 1
    assert jev.RECORD_CAP_ENV in str(e.value) and cap.reason == str(e.value)
    tiny_state, tiny_questions = {"p": 1}, {"q": {"type": "noul", "instructions": "x"}}
    with pytest.raises(jev.SpendCapReached):
        cap.judge(tiny_state, tiny_questions)   # once reached, nothing more leaves
    assert live.calls == 1 and cap.refused == 2
    assert cap.spent_usd == pytest.approx(_one_request_usd()) and cap.requests == 1


def test_spend_cap_counts_only_the_spend_since_it_was_made():
    jev.count_usage(50_000_000)         # 2.10 USD spent by something else first
    live = _Sized()
    cap = jev.SpendCap(live, cap_usd=_one_request_usd() * 1.5)
    cap.judge(STATE, QUESTIONS)
    assert live.calls == 1 and cap.spent_usd == pytest.approx(_one_request_usd())


def test_spend_cap_keeps_the_spend_made_before_a_reset_of_the_usage_counter():
    # a runner test calls jev.reset_usage() inside a recording: the spend
    # before it still counts, so the reset never raises the cap
    one = _one_request_usd()
    live = _Sized()
    cap = jev.SpendCap(live, cap_usd=one * 1.5)
    cap.judge(STATE, QUESTIONS)
    spent = cap.spent_usd
    jev.reset_usage()
    assert jev.usage()["requests"] == 0
    assert cap.spent_usd == pytest.approx(spent) == pytest.approx(one)
    with pytest.raises(jev.SpendCapReached):
        cap.judge(STATE, QUESTIONS)     # the spend before the reset plus this one's estimate
    assert live.calls == 1, "the request the cap refuses never leaves"


def test_spend_cap_requests_are_unchanged_by_a_reset():
    one = _one_request_usd()
    cap = jev.SpendCap(_Sized(), cap_usd=one * 10)
    cap.judge(STATE, QUESTIONS)
    cap.judge(STATE, QUESTIONS)
    jev.reset_usage()
    assert cap.requests == 2
    cap.judge(STATE, QUESTIONS)
    assert cap.requests == 3 and cap.spent_usd == pytest.approx(3 * one)


def test_spend_cap_counts_a_failed_request_when_a_reset_ran_inside_it():
    # the inner judge resets the counter and raises before counting: the
    # request may still have been billed, so it counts at its estimate
    class _CountedResetFailed:
        def judge(self, state, questions):
            jev.count_usage(1)
            jev.reset_usage()
            raise TimeoutError("the service timed out after the request left")

    class _ResetFailed:
        def judge(self, state, questions):
            jev.reset_usage()
            raise TimeoutError("the service timed out after the request left")

    jev.count_usage(5)                  # the resettable counter is above 0 at the start
    counted = jev.SpendCap(_CountedResetFailed(), cap_usd=1.0)
    with pytest.raises(TimeoutError):
        counted.judge(STATE, QUESTIONS)
    assert counted.failed == 0 and counted.requests == 1, "the counter saw it: no second count"
    jev.count_usage(5)
    uncounted = jev.SpendCap(_ResetFailed(), cap_usd=1.0)
    with pytest.raises(TimeoutError):
        uncounted.judge(STATE, QUESTIONS)
    assert uncounted.failed == 1 and uncounted.requests == 1
    assert uncounted.spent_usd == pytest.approx(_one_request_usd())


def test_the_session_accounting_survives_a_reset_mid_session(tmp_path, monkeypatch):
    # a recording made 9 live requests and reported 2: a runner test reset
    # the counter between them
    live = _Bumping(tokens=15_000_000)      # 0.63 USD per call
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0,
                            env={jev.KEY_ENV: "k-test"})
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    s.end("t::a")
    s.begin("t::b")
    jev.reset_usage()                       # a test that resets the counter
    s.end("t::b")
    rec = s.begin("t::c")
    s.judge().judge({"other": 1}, QUESTIONS)
    jev.reset_usage()                       # a reset inside the test that spent
    s.end("t::c")
    assert live.calls == 2
    assert s.requests == 2 and s.spent_usd == pytest.approx(1.26)
    assert rec.usd == pytest.approx(0.63) and s.records["t::b"].usd == 0.0
    assert s.cap.requests == 2 and s.cap.spent_usd == pytest.approx(1.26)
    assert s.live_usage() == {"requests": 2, "input_tokens": 30_000_000,
                              "usd": pytest.approx(1.26)}
    reason = s.skip_reason()
    assert reason and jev_harness.CAP_ENV in reason and "1.26" in reason


def test_the_session_live_usage_counts_only_since_the_session_began(tmp_path):
    jev.count_usage(50_000_000)             # spent before the session
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=1.0, env={}, dry=True)
    assert s.live_usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    jev.reset_usage()
    s.end("t::a")
    size = jev.request_size(STATE, QUESTIONS)[1]
    assert s.live_usage() == {"requests": 1, "input_tokens": size, "usd": jev.usd_for(size)}


def test_spend_cap_is_a_replay_miss_for_the_harness_and_a_judge_unavailable_for_the_run():
    assert issubclass(jev.SpendCapReached, jev.JevUnavailable)


def test_dry_run_answers_like_the_fake_and_counts_each_request_at_its_estimate():
    dry = jev_doubles.DryRun()
    answers = dry.judge(STATE, QUESTIONS)
    fake = jev.FakeJev().judge(STATE, QUESTIONS)
    assert {q: a.to_dict() for q, a in answers.items()} == {q: a.to_dict()
                                                            for q, a in fake.items()}
    size = jev.request_size(STATE, QUESTIONS)[1]
    assert (dry.requests, dry.tokens) == (1, size)
    # simulated: the live counters never see it
    assert jev.usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}


class _BilledThenFailed:
    """A live judge whose request the service may have billed, then the
    answer never came back (a timeout, a 5xx): it raises before counting."""

    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        raise TimeoutError("the service timed out after the request left")


def test_spend_cap_counts_a_request_that_failed_after_it_left():
    # a request that raised before the counter saw it counts
    # at its estimate, so the cap never runs behind the bill
    one = _one_request_usd()
    live = _BilledThenFailed()
    cap = jev.SpendCap(live, cap_usd=one * 1.5)
    with pytest.raises(TimeoutError):
        cap.judge(STATE, QUESTIONS)
    assert cap.spent_usd == pytest.approx(one) and cap.requests == 1 and cap.failed == 1
    with pytest.raises(jev.SpendCapReached):
        cap.judge(STATE, QUESTIONS)         # the failed one's estimate plus this one's
    assert live.calls == 1


def test_spend_cap_counts_a_counted_request_that_then_failed_once():
    # the counter saw it (the answer failed to parse after the count): no
    # second count
    class _CountedThenFailed(_Sized):
        def judge(self, state, questions):
            super().judge(state, questions)
            raise ValueError("an answer the parser could not read")
    cap = jev.SpendCap(_CountedThenFailed(), cap_usd=1.0)
    with pytest.raises(ValueError):
        cap.judge(STATE, QUESTIONS)
    assert cap.spent_usd == pytest.approx(_one_request_usd()) and cap.failed == 0
    assert cap.requests == 1


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "0", "0.0", "-0.05", "cheap"])
def test_the_cap_refuses_a_value_that_is_no_amount_above_zero(raw):
    # a NaN cap never stops (no sum is greater than NaN)
    with pytest.raises(ValueError, match=jev.RECORD_CAP_ENV):
        jev.record_cap({jev.RECORD_CAP_ENV: raw})
    with pytest.raises(ValueError):
        jev.SpendCap(_Sized(), cap_usd=float(raw) if raw != "cheap" else raw)


def _record_script(tmp_path, *args):
    """scripts/jev_record.ps1 run from a copy under `tmp_path`: its root
    holds no .env and the environment no key, so no run can reach the
    judge; the script stops at its cap check or at the missing key."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "jev_record.ps1", tmp_path / "scripts" / "jev_record.ps1")
    env = {k: v for k, v in os.environ.items()
           if k not in (jev.KEY_ENV, jev.RECORD_CAP_ENV)}
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(tmp_path / "scripts" / "jev_record.ps1"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(tmp_path), env=env, timeout=60)


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None,
                    reason="scripts/jev_record.ps1 is a Windows PowerShell script")
@pytest.mark.parametrize("args,said", [
    ((), "needs -Cap"),                                     # a live recording names its cap
    (("-Target", "matrix"), "needs -Cap"),
    (("-Cap", "NaN"), "finite USD amount above 0"),
    (("-Cap", "0"), "finite USD amount above 0"),
    (("-Cap", "-0.05"), "finite USD amount above 0"),
    (("-Cap", "0.05"), "TYPESAFE_API_KEY is not set"),     # past the cap check: the key's
])
def test_the_record_script_takes_a_live_recording_only_with_a_cap_above_zero(tmp_path, args,
                                                                             said):
    # one run with -Cap left out could spend the whole
    # approval; the cap is checked before the key is read
    res = _record_script(tmp_path, *args)
    assert res.returncode == 2, res.stdout + res.stderr
    assert said in res.stdout, res.stdout + res.stderr


def test_the_cap_env_parses_and_only_a_dry_run_has_a_default():
    # a live recording names its cap; only the dry run has a default, the
    # fixed DRY_RECORD_CAP_USD
    with pytest.raises(ValueError, match=jev.RECORD_CAP_ENV):
        jev.record_cap({})
    with pytest.raises(ValueError, match=jev.RECORD_CAP_ENV):
        jev.record_cap({jev.RECORD_CAP_ENV: "  "})
    assert jev.record_cap({}, live=False) == jev.DRY_RECORD_CAP_USD == 0.88
    assert jev.record_cap({jev.RECORD_CAP_ENV: " 0.35 "}) == 0.35
    assert jev.record_cap({jev.RECORD_CAP_ENV: " 0.35 "}, live=False) == 0.35
    assert jev_harness.dry_from({jev_harness.DRY_ENV: "1"})
    assert jev_harness.dry_from({jev_harness.DRY_ENV: "Yes"})
    assert not jev_harness.dry_from({}) and not jev_harness.dry_from({jev_harness.DRY_ENV: "0"})


def test_record_mode_asks_the_live_judge_through_the_cap(tmp_path, monkeypatch):
    live = _Sized()
    monkeypatch.setattr(jev_harness, "live_judge", lambda: live)
    s = jev_harness.Session("record", tmp_path / "cache.json", cap_usd=0.5,
                            env={jev.KEY_ENV: "k-test"})
    s.begin("t::a")
    judge = s.judge()
    assert isinstance(judge.inner.inner, jev.SpendCap) and judge.inner.inner.inner is live
    assert s.cap is judge.inner.inner and s.cap.cap_usd == 0.5


def test_a_dry_record_needs_no_key_and_leaves_the_cache_as_it_was(tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(jev.FakeJev(), cache).judge({"other": 1}, QUESTIONS)
    before = cache.read_bytes()
    s = jev_harness.Session("record", cache, cap_usd=1.0, env={}, dry=True)
    assert s.dry and s.skip_reason() is None
    assert s.source_cache == cache and s.cache_path != cache
    assert s.cached_count() == 1, "the temp copy starts from the cache"
    s.begin("t::a")
    s.judge().judge(STATE, QUESTIONS)
    s.end("t::a")
    assert isinstance(s.cap.inner, jev_doubles.DryRun) and s.cap.inner.requests == 1
    assert s.requests == 1 and s.spent_usd == pytest.approx(_one_request_usd())
    assert cache.read_bytes() == before
    assert jev_harness.outcomes_path(s.cache_path).parent != cache.parent
    assert not jev_harness.dry_from({}) and not jev_harness.Session(
        "replay", cache, env={}, dry=True).dry, "dry is a record mode's only"


def test_a_dry_record_stops_at_the_cap_before_the_request_leaves(tmp_path):
    s = jev_harness.Session("record", tmp_path / "cache.json",
                            cap_usd=_one_request_usd() * 0.5, env={}, dry=True)
    rec = s.begin("t::a")
    with pytest.raises(jev.SpendCapReached):
        s.judge().judge(STATE, QUESTIONS)
    s.end("t::a")
    assert rec.capped and jev.RECORD_CAP_ENV in rec.capped and rec.misses == []
    assert s.requests == 0 and s.cap.inner.requests == 0
    reason = s.skip_reason()
    assert reason and jev_harness.CAP_ENV in reason and s.stopped == reason


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


# --- the xdist guard on record/replay -------------------------------------------------

class _FakeOption:
    def __init__(self, numprocesses=None):
        self.numprocesses = numprocesses


class _FakeConfig:
    """Just enough of a real `pytest.Config` for `conftest_jev.pytest_configure`: a
    real `Stash` (so `SESSION_KEY in config.stash` behaves exactly as it does for
    pytest itself) and `option.numprocesses`. `workerinput` is set only when standing
    in for a worker's own config -- a real worker's config always carries it (set by
    `xdist/remote.py` before any hook runs there); a real controller's never does."""

    def __init__(self, numprocesses=None, worker=False):
        self.stash = pytest.Stash()
        self.option = _FakeOption(numprocesses)
        if worker:
            self.workerinput = {"workerid": "gw0"}
        self.ini_lines: list[tuple[str, str]] = []

    def addinivalue_line(self, name, line):
        self.ini_lines.append((name, line))


@pytest.fixture
def cjev():
    """The `conftest_jev` module, imported lazily (function scope, after collection)
    so this never races pytest's own assertion-rewriting import of it as the
    `conftest_jev` plugin (a module-level `import conftest_jev` here would run before
    pytest processes this file's `pytest_plugins` and gets a "module already imported,
    cannot be rewritten" warning)."""
    import conftest_jev
    return conftest_jev


def test_configure_refuses_record_mode_on_an_xdist_worker(cjev, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev.KEY_ENV, "k-test")
    monkeypatch.setenv(jev.RECORD_CAP_ENV, "0.5")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    cfg = _FakeConfig(worker=True)    # this config's own workerinput: a real worker
    with pytest.raises(pytest.UsageError) as exc:
        cjev.pytest_configure(cfg)
    assert jev_harness.serial_command("record") in str(exc.value)
    assert not (tmp_path / "cache.json").exists()
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_configure_refuses_a_live_recording_that_names_no_cap(cjev, monkeypatch, tmp_path):
    # the run stops before any test, with the variable named
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev.KEY_ENV, "k-test")
    monkeypatch.delenv(jev.RECORD_CAP_ENV, raising=False)
    monkeypatch.delenv(jev_harness.DRY_ENV, raising=False)
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    cfg = _FakeConfig()
    with pytest.raises(pytest.UsageError, match=jev.RECORD_CAP_ENV):
        cjev.pytest_configure(cfg)
    assert cjev.SESSION_KEY not in cfg.stash
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_configure_refuses_replay_mode_on_the_xdist_controller(cjev, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    cfg = _FakeConfig(numprocesses=4)    # -n already resolved from "auto"; no workerinput
    with pytest.raises(pytest.UsageError) as exc:
        cjev.pytest_configure(cfg)
    assert jev_harness.serial_command("replay") in str(exc.value)
    assert not (tmp_path / "outcomes.jsonl").exists()


def test_configure_ignores_xdist_in_fake_mode(cjev, monkeypatch, tmp_path):
    monkeypatch.delenv(jev_harness.MODE_ENV, raising=False)
    cfg = _FakeConfig(worker=True)
    cjev.pytest_configure(cfg)    # must not raise: fake mode never touches the cache
    assert cjev.SESSION_KEY in cfg.stash


def test_configure_runs_record_mode_serially(cjev, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev.KEY_ENV, "k-test")
    monkeypatch.setenv(jev.RECORD_CAP_ENV, "0.5")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    cfg = _FakeConfig()           # no -n: neither a worker nor a busy controller
    cjev.pytest_configure(cfg)    # must not raise
    assert (tmp_path / "outcomes.jsonl").exists()


def test_configure_runs_replay_mode_serially_with_n_explicitly_zero(cjev, monkeypatch, tmp_path):
    """`-n 0` is xdist's own no-op spelling (same as omitting -n): numprocesses is 0,
    not falsy-but-set, so the controller check must not fire on it."""
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    cfg = _FakeConfig(numprocesses=0)
    cjev.pytest_configure(cfg)    # must not raise
    assert (tmp_path / "outcomes.jsonl").exists()


def test_configure_ignores_an_xdist_worker_env_var_inherited_by_a_nested_run(
        cjev, monkeypatch, tmp_path):
    """A `pytester.runpytest_inprocess(...)` call nested inside a real xdist worker
    (as the tests below do) inherits that worker's `PYTEST_XDIST_WORKER[_COUNT]` env
    vars -- same process, same `os.environ` -- even though the nested, single-process
    pytest run it starts is never itself distributed. The guard must key off THIS
    config (no `workerinput`, no `-n` of its own), not off an inherited env var, or
    every `pytester`-based test below starts failing under `-n` (as running this
    file with `-n 2` shows)."""
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "2")
    cfg = _FakeConfig()            # this config itself: no workerinput, no -n
    cjev.pytest_configure(cfg)     # must not raise
    assert (tmp_path / "outcomes.jsonl").exists()


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
    assert "jev_record.ps1 -Target runner -Cap <USD>" in out


_INNER_UNRECORDED = '''
import pytest
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

@pytest.mark.jev_unrecorded
def test_marked(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)

def test_unmarked(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)
'''


def test_a_marked_tests_replay_miss_skips_until_sp8_records_it(pytester, monkeypatch,
                                                              tmp_path):
    # a test marked as not yet recorded skips on its replay miss (no live
    # request); an unmarked miss still fails
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_UNRECORDED.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider",
                                          "-p", "conftest_jev",
                                          "-W", "error::pytest.PytestUnknownMarkWarning")
    result.assert_outcomes(skipped=1, failed=1)
    assert jev_harness.UNRECORDED_REASON in result.stdout.str()
    assert jev_harness.UNRECORDED_REASON == "no replay recording yet; scripts/jev_record.ps1 records it"


def test_a_marked_test_runs_in_fake_mode(pytester, monkeypatch, tmp_path):
    monkeypatch.delenv(jev_harness.MODE_ENV, raising=False)
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_UNRECORDED.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider", "-p", "conftest_jev",
                                          "-W", "error::pytest.PytestUnknownMarkWarning")
    result.assert_outcomes(passed=2)


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


_INNER_LATER_DAY = '''
import datetime
import apply_facts
pytest_plugins = ["conftest_jev"]

STATE = STATE_HERE
QUESTIONS = QUESTIONS_HERE


class _Later(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 1)


def test_a_later_day(jev_judge, tmp_path, monkeypatch):
    monkeypatch.setattr(apply_facts, "date", _Later)
    catalog = apply_facts.build(tmp_path, answers=[])
    jev_judge().judge(dict(STATE, facts=catalog.verification_excerpt(["today"])), QUESTIONS)
    assert catalog.value("today") == EXPECTED_HERE
'''


def _inner_later_day(pytester, expected: str):
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_LATER_DAY.replace("STATE_HERE", repr(STATE))
                        .replace("QUESTIONS_HERE", repr(QUESTIONS))
                        .replace("EXPECTED_HERE", repr(expected)))


def test_a_replay_whose_clock_reads_a_later_day_still_hits_the_cache(
        pytester, monkeypatch, tmp_path):
    # the catalog lists today's date in every request that carries the
    # facts, so a replay on a later day would miss each one unpinned.
    # The cache holds a request recorded on the pinned day, the inner test's
    # clock reads 2026-10-01, and the replay still hits it.
    import apply_facts
    cache = tmp_path / "cache.json"
    catalog = apply_facts.build(tmp_path, answers=[], today=jev_harness.RECORDED_TODAY)
    jev.ReplayJev(jev.FakeJev(), cache).judge(
        dict(STATE, facts=catalog.verification_excerpt(["today"])), QUESTIONS)
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    _inner_later_day(pytester, jev_harness.RECORDED_TODAY.isoformat())
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert "replay hits 1, misses 0" in result.stdout.str()


def test_the_fake_mode_catalog_keeps_the_clocks_own_date(pytester, monkeypatch, tmp_path):
    monkeypatch.delenv(jev_harness.MODE_ENV, raising=False)
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner_later_day(pytester, "2026-10-01")
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert not (tmp_path / "cache.json").exists()


def test_fixture_in_record_mode_without_a_key_skips_every_test(pytester, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    monkeypatch.setenv(jev.RECORD_CAP_ENV, "0.5")
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


def test_fixture_in_a_dry_record_skips_the_test_the_cap_stops_and_every_one_after(
        pytester, monkeypatch, tmp_path):
    # the cap proved on the fake: the first request's estimate is over the
    # cap, so nothing is answered, nothing is written to the cache, and each
    # test skips with the cap's reason (never a failure, never a divergence)
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev_harness.DRY_ENV, "1")
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    monkeypatch.setenv(jev_harness.CAP_ENV, f"{_one_request_usd() * 0.5:.10f}")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=3)
    out = result.stdout.str()
    assert jev_harness.CAP_ENV in out and "dry run" in out
    assert "estimated live requests 0" in out
    assert not (tmp_path / "cache.json").exists()
    assert not (tmp_path / "outcomes.jsonl").exists(), "a dry run writes beside its temp copy"


def test_fixture_in_a_dry_record_estimates_the_requests_a_recording_makes(
        pytester, monkeypatch, tmp_path):
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev_harness.DRY_ENV, "1")
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    monkeypatch.setenv(jev_harness.CAP_ENV, "1.00")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    _inner(pytester)
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=1)
    out = result.stdout.str()
    # the three tests ask one request between them: the first asks, the rest replay
    assert "estimated live requests 1" in out and "jev record (dry run)" in out
    assert not (tmp_path / "cache.json").exists()


_INNER_RESET = '''
import jev
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_one(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)
    jev.reset_usage()

def test_two(jev_judge):
    jev_judge().judge({{"other": 1}}, QUESTIONS)

def test_three(jev_judge):
    jev.reset_usage()
'''


def test_the_record_summary_counts_every_live_request_across_a_reset(
        pytester, monkeypatch, tmp_path):
    # the summary read the resettable counter: a recording made 2 requests
    # and reported 0 after a runner test reset it
    monkeypatch.setenv(jev_harness.MODE_ENV, "record")
    monkeypatch.setenv(jev_harness.DRY_ENV, "1")
    monkeypatch.delenv(jev.KEY_ENV, raising=False)
    monkeypatch.setenv(jev_harness.CAP_ENV, "1.00")
    monkeypatch.setenv(jev.CACHE_ENV, str(tmp_path / "cache.json"))
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_RESET.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)
    tokens = (jev.request_size(STATE, QUESTIONS)[1]
              + jev.request_size({"other": 1}, QUESTIONS)[1])
    out = result.stdout.str()
    assert f"estimated live requests 2, {tokens} input tokens" in out, out


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


def _answer(kind, value, choice=None):
    if kind == "noul":
        return jev.Answer(kind="noul", noul=value).to_dict()
    return jev.Answer(kind="choice", choice=choice, probabilities={choice: value},
                      confidence=value).to_dict()


def test_thresholds_helper_reads_todays_question_ids_over_several_caches(tmp_path):
    # the page read's Nouls, the send Noul, an error's field, the
    # inbox's link Noul and the link pick are today's ids; each cache adds
    # its requests, and the combined reads come from a matrix --json file
    # and the captures' results
    first = tmp_path / "a" / "cache.json"
    second = tmp_path / "b" / "cache.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(json.dumps({"k1": {
        "page_state": _answer("choice", 0.95, "application_form"),
        "page_applicant_details": _answer("noul", 0.97),
        "page_sign_in": _answer("noul", 0.03),
        "button_0_sends": _answer("noul", 0.91),
        "error_0_field": _answer("choice", 0.66, "q2")}}), encoding="utf-8")
    second.write_text(json.dumps({"k2": {
        "msg_0_has_link": _answer("noul", 0.93),
        "msg_0_from_site": _answer("noul", 0.88),
        "link_pick": _answer("choice", 0.99, "link_1")}}), encoding="utf-8")
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps([{"flow": "ashby_wizard", "judge": "real", "reads": [
        {"n": 1, "state": "application_form", "conf": 0.93, "read": "application_form",
         "read_conf": 0.93, "judged": "application_form", "judged_conf": 1.0},
        {"n": 2, "state": "confirmation", "conf": 0.35, "read": "confirmation",
         "read_conf": 0.35, "judged": "review_page", "judged_conf": 0.6}]}]), encoding="utf-8")
    captures = tmp_path / "results.json"
    captures.write_text(json.dumps({"reads": [
        {"capture": "x/posting", "expected": ["job_posting"], "read": "job_posting",
         "read_conf": 0.8, "judged": "job_posting", "judged_conf": 0.9},
        {"capture": "x/apply", "expected": ["application_form"], "read": "other",
         "read_conf": 0.45, "judged": "other", "judged_conf": 0.5}]}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "jev_thresholds.py"),
                           "--cache", str(first), "--cache", str(second), "--no-outcomes",
                           "--reads", str(matrix), "--reads", str(captures)],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "jev answers: 8 over 2 request(s)" in out
    assert "read page_applicant_details: 1 answer(s); gate READ_NOUL_MIN = 0.50" in out
    assert "button sends: 1 answer(s); gate BUTTON_SENDS_MIN = 0.40" in out
    assert "error field: 1 answer(s); gate FIELD_MAP_MIN_CONF = 0.70" in out
    assert "1 of 1 below the gate" in out
    assert "inbox: 2 answer(s)" in out and "link pick: 1 answer(s)" in out
    assert "unrecognised id" not in out
    assert "combined page reads: 4 read(s)" in out
    assert "confirmation: 1;" in out and "1 under the gate" in out
    assert "against the labels: 1 right" in out and "1 wrong" in out
    assert "ashby_wizard real p2: judged review_page 0.60, read confirmation 0.35" in out
    assert "outcomes: 0 test(s) recorded" in out


def test_the_record_scripts_runner_target_runs_the_runner_tests():
    # the difficulty check's jev_unrecorded tests are recorded with the
    # runner tests; the script and RUNNER_TESTS name
    # the same files, and every one exists
    files = jev_harness.RUNNER_TESTS.split()
    assert "tests/test_apply_assess.py" in files
    assert all((REPO / f).is_file() for f in files), files
    script = (REPO / "scripts" / "jev_record.ps1").read_text(encoding="ascii")
    runs = [line.strip() for line in script.splitlines()
            if line.strip().startswith("python -m pytest tests/test_apply_run.py")]
    assert runs == [f"python -m pytest {jev_harness.RUNNER_TESTS} -q"], runs


def _record_script_in_caller(tmp_path, *args, qt=None):
    """Run scripts/jev_record.ps1 (copied under `tmp_path`) from a caller shell
    sitting in `tmp_path/caller`, with a fake `python` first on PATH so no
    test, judge or network is reached; print the caller's location, its
    QT_QPA_PLATFORM and the cap variable afterwards, one per line."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "jev_record.ps1", tmp_path / "scripts" / "jev_record.ps1")
    caller = tmp_path / "caller"
    caller.mkdir()
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "python.bat").write_text("@echo fake python %*\r\n@exit /b 0\r\n", encoding="ascii")
    env = {k: v for k, v in os.environ.items()
           if k not in (jev.KEY_ENV, jev.RECORD_CAP_ENV, "QT_QPA_PLATFORM")}
    env["PATH"] = str(fake) + os.pathsep + env.get("PATH", "")
    if qt is not None:
        env["QT_QPA_PLATFORM"] = qt
    script = str(tmp_path / "scripts" / "jev_record.ps1")
    command = (f"Set-Location '{caller}'; & '{script}' {' '.join(args)}; "
               "Write-Output (Get-Location).Path; "
               "Write-Output (\"qt=\" + $env:QT_QPA_PLATFORM); "
               f"Write-Output (\"cap=\" + $env:{jev.RECORD_CAP_ENV})")
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=60)
    return caller, proc.stdout.strip().splitlines()[-3:], proc


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None,
                    reason="scripts/jev_record.ps1 is a Windows PowerShell script")
@pytest.mark.parametrize("args,said", [
    (("-Cap", "0.05"), "TYPESAFE_API_KEY is not set"),     # stops at the missing key
    (("-Mode", "replay"), "fake python -m pytest"),        # runs the fake python to the end
])
def test_the_record_script_leaves_the_callers_location_and_environment(tmp_path, args, said):
    caller, lines, proc = _record_script_in_caller(tmp_path, *args)
    assert said in proc.stdout, proc.stdout + proc.stderr
    assert lines[0] == str(caller), proc.stdout + proc.stderr
    assert lines[1] == "qt=", proc.stdout + proc.stderr
    assert lines[2] == "cap=", proc.stdout + proc.stderr


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None,
                    reason="scripts/jev_record.ps1 is a Windows PowerShell script")
def test_the_record_script_restores_a_callers_own_qt_platform(tmp_path):
    _caller, lines, proc = _record_script_in_caller(tmp_path, "-Mode", "replay", qt="windows")
    assert "fake python -m pytest" in proc.stdout, proc.stdout + proc.stderr
    assert lines[1] == "qt=windows", proc.stdout + proc.stderr


def test_the_record_script_sets_no_cap_a_run_did_not_name():
    """A replay or a dry run left without -Cap sets no cap variable, so the
    Python side's own dry default applies and the script carries none."""
    text = (REPO / "scripts" / "jev_record.ps1").read_text(encoding="ascii")
    assert "0.88" not in text
    assert "[double]$Cap = " not in text

# --- simulated requests stay off the live counters --------------------------------------

def _delta(before, after):
    return {k: after[k] - before[k] for k in ("requests", "input_tokens")}


def test_a_dry_run_counts_on_the_simulated_counter_and_never_on_the_live_ones():
    live0, total0, sim0 = jev.usage(), jev.total_usage(), jev.simulated_usage()
    jev_doubles.DryRun().judge(STATE, QUESTIONS)
    size = jev.request_size(STATE, QUESTIONS)[1]
    assert _delta(live0, jev.usage()) == {"requests": 0, "input_tokens": 0}
    assert _delta(total0, jev.total_usage()) == {"requests": 0, "input_tokens": 0}
    assert _delta(sim0, jev.simulated_usage()) == {"requests": 1, "input_tokens": size}
    both0 = jev.total_usage(include_simulated=True)
    jev_doubles.DryRun().judge(STATE, QUESTIONS)
    jev.count_usage(7)
    assert _delta(both0, jev.total_usage(include_simulated=True)) == {
        "requests": 2, "input_tokens": size + 7}


def test_a_live_spend_cap_ignores_simulated_requests_and_still_counts_a_real_one():
    one = _one_request_usd()
    live = _Sized()
    cap = jev.SpendCap(live, cap_usd=one * 1.5)
    for _ in range(5):
        jev_doubles.DryRun().judge(STATE, QUESTIONS)     # a dry run elsewhere in the process
    assert cap.spent_usd == 0.0 and cap.requests == 0
    cap.judge(STATE, QUESTIONS)
    assert live.calls == 1
    assert cap.spent_usd == pytest.approx(one) and cap.requests == 1
    with pytest.raises(jev.SpendCapReached):
        cap.judge(STATE, QUESTIONS)              # the real request still counts
    assert live.calls == 1


def test_a_dry_spend_cap_counts_its_simulated_requests_and_any_real_one():
    one = _one_request_usd()
    cap = jev.SpendCap(jev_doubles.DryRun(), cap_usd=one * 10)
    cap.judge(STATE, QUESTIONS)
    assert cap.requests == 1 and cap.spent_usd == pytest.approx(one)
    jev.count_usage(jev.request_size(STATE, QUESTIONS)[1])      # a real one besides
    assert cap.requests == 2 and cap.spent_usd == pytest.approx(2 * one)


def test_spend_cap_counts_a_billed_response_that_reported_no_input_tokens():
    # the service answered (one request counted) but its usage said 0 input
    # tokens: the request counts at its estimate, so the cap never runs behind
    class _NoTokens:
        calls = 0

        def judge(self, state, questions):
            self.calls += 1
            jev.count_usage(0)
            return jev.FakeJev().judge(state, questions)
    one = _one_request_usd()
    live = _NoTokens()
    cap = jev.SpendCap(live, cap_usd=one * 1.5)
    cap.judge(STATE, QUESTIONS)
    assert cap.requests == 1 and cap.spent_usd == pytest.approx(one)
    with pytest.raises(jev.SpendCapReached):
        cap.judge(STATE, QUESTIONS)
    assert live.calls == 1


def test_spend_cap_checks_and_asks_as_one_step_across_threads():
    # two threads each pass the check before either request is counted:
    # the cap lets both leave unless the check and the request are one step
    import threading
    import time as _time

    class _Slow(_Sized):
        def judge(self, state, questions):
            _time.sleep(0.2)
            return super().judge(state, questions)
    one = _one_request_usd()
    live = _Slow()
    cap = jev.SpendCap(live, cap_usd=one * 1.5)
    refused = []

    def ask():
        try:
            cap.judge(STATE, QUESTIONS)
        except jev.SpendCapReached:
            refused.append(1)
    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert live.calls == 1 and refused == [1]


_INNER_SIMULATED = '''
import jev
import jev_doubles
pytest_plugins = ["conftest_jev"]

STATE = {STATE!r}
QUESTIONS = {QUESTIONS!r}

def test_replays(jev_judge):
    jev_judge().judge(STATE, QUESTIONS)

def test_simulates(jev_judge):
    jev_doubles.DryRun().judge(STATE, QUESTIONS)
    jev_doubles.DryRun().judge(STATE, QUESTIONS)

def test_counts_like_the_live_client(jev_judge):
    jev.count_usage(5)
'''


def test_the_replay_summary_never_reports_a_simulated_request_as_a_live_one(
        pytester, monkeypatch, tmp_path):
    # a test in the replay run drove `jev_doubles.DryRun` (the scorer's summary
    # test does): the summary said "live requests 2" though replay sends nothing
    cache = tmp_path / "cache.json"
    jev.ReplayJev(jev.FakeJev(), cache).judge(STATE, QUESTIONS)
    monkeypatch.setenv(jev_harness.MODE_ENV, "replay")
    monkeypatch.setenv(jev.CACHE_ENV, str(cache))
    pytester.syspathinsert(TESTS)
    pytester.syspathinsert(REPO / "local")
    pytester.makepyfile(test_inner=_INNER_SIMULATED.format(STATE=STATE, QUESTIONS=QUESTIONS))
    result = pytester.runpytest_inprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)
    out = result.stdout.str()
    size = jev.request_size(STATE, QUESTIONS)[1]
    assert "replay hits 1, misses 0; live requests 0 (replay sends nothing)" in out, out
    assert ("test doubles counted 1 request(s) as live, 5 input tokens (never sent, "
            "not billed)") in out, out
    assert (f"simulated DryRun requests 2, ~{2 * size} est. input tokens, "
            f"est. {jev.usd_for(2 * size):.4f} USD (not billed)") in out, out
