"""Tests for local/jev.py: the TypeSafe (Jev) client wrapper, its key-free fake,
the replay cache and the factory.

Nothing here reaches the network. `TypeSafeJev` is exercised against a fake
`typesafe_sdk` module installed into `sys.modules`, and every other test uses
`FakeJev`, whose rules the class docstring pins so later phases can write
fixtures against them.
"""
import json
import logging
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "AUTO_APPLY_JEV_MODE", "AUTO_APPLY_JEV_CACHE"):
        monkeypatch.delenv(var, raising=False)
    jev.reset_usage()
    yield
    jev.reset_usage()


STATE = {
    "page": {"title": "Application for Data Engineer", "url": "https://jobs.example/apply"},
    "field": {"label": "Are you legally authorized to work in the United States?",
              "options": ["Yes", "No"]},
    "sheet": {"work_authorization": "US citizen, authorized to work without sponsorship",
              "phone": "555-0100"},
}

QUESTIONS = {
    "is_captcha": {"type": "noul",
                   "instructions": "Does `page` show a CAPTCHA or robot check?"},
    "authorized": {"type": "noul",
                   "instructions": "Does `sheet.work_authorization` say the candidate is "
                                   "legally authorized to work in the United States?"},
    "which_option": {"type": "choice",
                     "instructions": "Which option in `field.options` matches "
                                     "`sheet.work_authorization`?",
                     "criteria": {"yes": "A citizen or permanent resident, authorized to work",
                                  "no": "Needs a visa",
                                  "none": "No option fits"}},
    "fit": {"type": "score",
            "instructions": "How well does `sheet` cover `field.label`?",
            "criteria": ["Nothing in the sheet answers the field",
                         "The sheet answers the field partly",
                         "The sheet answers the field: work authorization is stated"]},
}


# --- constants and the Answer shape -------------------------------------------

def test_model_and_price_are_pinned():
    assert jev.MODEL == "jev-1.13.0"
    assert jev.PRICE_USD_PER_MTOK == 0.042


def test_answer_dataclass_fields_and_defaults():
    a = jev.Answer(kind="noul", noul=0.9)
    assert (a.kind, a.noul, a.choice, a.score) == ("noul", 0.9, None, None)
    assert a.probabilities == {} and a.confidence is None


# --- (a) FakeJev: an Answer per id with the right kind --------------------------

def test_fake_returns_an_answer_per_question_id_with_the_right_kind():
    out = jev.get("fake").judge(STATE, QUESTIONS)
    assert set(out) == set(QUESTIONS)
    for qid, q in QUESTIONS.items():
        assert isinstance(out[qid], jev.Answer)
        assert out[qid].kind == q["type"]


def test_fake_noul_is_high_when_the_instruction_words_appear_in_the_state():
    out = jev.FakeJev().judge(STATE, QUESTIONS)
    assert out["authorized"].noul == 0.9      # "authorized" and "work" are in the slice
    assert out["is_captcha"].noul == 0.1      # "captcha", "robot", "check": none in the state
    assert out["authorized"].confidence is None and out["authorized"].probabilities == {}


def test_fake_reads_the_first_word_of_every_line_in_a_json_slice():
    """A state slice is serialised as JSON, where a newline is the two
    characters backslash and n; the fake must not glue that escape onto the
    next word (a JSON-escaped "Thank" is "thank", never "nthank")."""
    state = {"page": {"headline_text": "Analytics Engineer\nApplication received\nThank you"}}
    q = {"done": {"type": "noul",
                  "instructions": "Does `page` say the application was received with a thank?"}}
    assert jev.FakeJev().judge(state, q)["done"].noul == 0.9


def test_fake_choice_picks_the_option_with_the_most_word_overlap():
    a = jev.FakeJev().judge(STATE, QUESTIONS)["which_option"]
    assert a.choice == "yes"
    assert a.probabilities == {"yes": 1.0, "no": 0.0, "none": 0.0}
    assert a.confidence == 1.0


def test_fake_choice_falls_back_in_priority_order_when_no_option_overlaps():
    q = {"pick": {"type": "choice", "instructions": "zzz",
                  "criteria": {"alpha": None, "no_match": None, "other": None,
                               "none": None, "leave_blank": None}}}
    assert jev.FakeJev().judge("qqq", q)["pick"].choice == "none"
    del q["pick"]["criteria"]["none"]
    assert jev.FakeJev().judge("qqq", q)["pick"].choice == "other"
    del q["pick"]["criteria"]["other"]
    assert jev.FakeJev().judge("qqq", q)["pick"].choice == "leave_blank"
    del q["pick"]["criteria"]["leave_blank"]
    assert jev.FakeJev().judge("qqq", q)["pick"].choice == "no_match"
    del q["pick"]["criteria"]["no_match"]
    assert jev.FakeJev().judge("qqq", q)["pick"].choice == "alpha"


def test_fake_score_is_the_index_of_the_level_with_the_most_overlap():
    a = jev.FakeJev().judge(STATE, QUESTIONS)["fit"]
    assert a.kind == "score" and a.score == 2.0
    assert a.probabilities == {"0": 0.0, "1": 0.0, "2": 1.0}
    assert a.confidence == 1.0


def test_fake_reads_a_backticked_path_slice_of_the_state():
    """A path that names one slice keeps the rest of the state out of the
    overlap: `sheet.phone` says nothing about authorization."""
    q = {"n": {"type": "noul",
               "instructions": "Does `sheet.phone` state legal work authorization "
                               "for the United States?"}}
    assert jev.FakeJev().judge(STATE, q)["n"].noul == 0.1


def test_fake_accepts_structured_instructions_and_a_string_state():
    q = {"n": {"type": "noul",
               "instructions": {"question": "Is the candidate legally authorized?",
                                "focus": "work authorization"}}}
    out = jev.FakeJev().judge("US citizen, legally authorized to work", q)
    assert out["n"].noul == 0.9


def test_fake_reads_a_named_option_table_as_per_option_descriptions():
    """A backticked path that resolves to a map keyed by the Choice's option
    names is the options' description table: each option scores against its
    own entry, and the table stays out of the shared state text (otherwise
    every key's own words would be in the context and the earliest key with
    the most words would win)."""
    state = {"field": {"label": "Are you legally authorized to work in the US?",
                       "options": ["Yes", "No"]},
             "facts": {"gender": "The candidate's gender, for EEO self-identification",
                       "work_authorized": "Whether the candidate is legally authorized "
                                          "to work in the United States",
                       "address_state": "State or province of the mailing address",
                       "leave_blank": "no source applies"}}
    q = {"src": {"type": "choice",
                 "instructions": "Which key of `facts` describes what `field` asks for?",
                 "criteria": {"address_state": None, "gender": None,
                              "work_authorized": None, "leave_blank": None}}}
    assert jev.FakeJev().judge(state, q)["src"].choice == "work_authorized"
    # a map that shares no key with the options is ordinary state text
    q["src"]["criteria"] = {"alpha": "mailing address", "beta": "candidate gender", "none": None}
    assert jev.FakeJev().judge(state, q)["src"].choice == "alpha"


def test_fake_choice_not_for_words_count_against_the_option():
    """A structured description's `not_for` entry is a negative: each of its
    words found in the context subtracts one, and an option scoring at or
    below zero cannot win (the escape fallback applies when nothing is
    positive)."""
    q = {"src": {"type": "choice",
                 "instructions": "Which option describes `label`?",
                 "criteria": {
                     "consent": {"what": "a checkbox to confirm accuracy or consent to contact",
                                 "not_for": "background check, drug test"},
                     "leave_blank": None}}}
    fake = jev.FakeJev()
    assert fake.judge({"label": "I consent to be contacted"}, q)["src"].choice == "consent"
    assert fake.judge({"label": "I consent to a background check and drug test"},
                      q)["src"].choice == "leave_blank"
    a = fake.judge({"label": "I consent to a background check"}, q)["src"]
    assert a.choice == "leave_blank"      # one for, two against
    assert a.probabilities == {"consent": 0.0, "leave_blank": 1.0}


def test_fake_is_deterministic_and_never_touches_usage():
    first = jev.FakeJev().judge(STATE, QUESTIONS)
    second = jev.FakeJev().judge(STATE, QUESTIONS)
    assert first == second
    assert jev.usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}


# --- (b) ReplayJev: record on a miss, replay on a hit ---------------------------

class _Counting:
    """A Jev that counts calls and delegates to the fake."""

    def __init__(self):
        self.calls = 0
        self.inner = jev.FakeJev()

    def judge(self, state, questions):
        self.calls += 1
        return self.inner.judge(state, questions)


def test_replay_records_on_a_miss_and_replays_on_a_hit(tmp_path):
    inner = _Counting()
    cache = tmp_path / "jev_cache" / "cache.json"
    r = jev.ReplayJev(inner, cache)
    first = r.judge(STATE, QUESTIONS)
    assert inner.calls == 1 and cache.is_file()
    second = r.judge(STATE, QUESTIONS)
    assert inner.calls == 1, "a hit must not call the inner client again"
    assert second == first
    assert (r.hits, r.misses) == (1, 1)


def test_replay_hit_over_typesafe_leaves_usage_at_zero_after_the_replay(monkeypatch, tmp_path):
    # _sdk_response() only answers is_captcha, which_option and fit, so the
    # question set here must match it for the second call to land as a hit.
    q = {k: QUESTIONS[k] for k in ("is_captcha", "which_option", "fit")}
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    cache = tmp_path / "cache.json"
    r = jev.ReplayJev(jev.TypeSafeJev(api_key="k-test"), cache)
    r.judge(STATE, q)
    assert jev.usage()["requests"] == 1
    jev.reset_usage()
    r.judge(STATE, q)
    assert jev.usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}
    assert (r.hits, r.misses) == (1, 1)


def test_replay_survives_a_fresh_instance_over_the_same_file(tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(_Counting(), cache).judge(STATE, QUESTIONS)
    inner = _Counting()
    out = jev.ReplayJev(inner, cache).judge(STATE, QUESTIONS)
    assert inner.calls == 0
    assert out["which_option"].choice == "yes" and out["fit"].score == 2.0


def test_replay_key_is_the_sha256_of_the_canonical_request(tmp_path):
    cache = tmp_path / "cache.json"
    jev.ReplayJev(_Counting(), cache).judge(STATE, QUESTIONS)
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert list(stored) == [jev.ReplayJev.key_for(STATE, QUESTIONS)]
    assert len(list(stored)[0]) == 64
    # key order inside the state does not change the key
    flipped = {k: STATE[k] for k in reversed(list(STATE))}
    assert jev.ReplayJev.key_for(flipped, QUESTIONS) == jev.ReplayJev.key_for(STATE, QUESTIONS)
    # a different question does
    assert jev.ReplayJev.key_for(STATE, {"x": QUESTIONS["fit"]}) != list(stored)[0]


def test_replay_misses_on_a_changed_state(tmp_path):
    inner = _Counting()
    r = jev.ReplayJev(inner, tmp_path / "cache.json")
    r.judge(STATE, QUESTIONS)
    r.judge({**STATE, "page": {"title": "Other"}}, QUESTIONS)
    assert inner.calls == 2


# --- (b2) ReplayJev.used_keys and jev.prune_cache -------------------------------

def test_used_keys_starts_empty_and_gains_the_key_on_a_hit(tmp_path):
    cache = tmp_path / "cache.json"
    r = jev.ReplayJev(_Counting(), cache)
    assert r.used_keys == set()
    key = jev.ReplayJev.key_for(STATE, QUESTIONS)
    r.judge(STATE, QUESTIONS)               # a miss that records
    assert r.used_keys == {key}
    r2 = jev.ReplayJev(None, cache)
    r2.judge(STATE, QUESTIONS)              # a hit over a fresh instance
    assert r2.used_keys == {key}


def test_used_keys_holds_one_entry_per_distinct_request(tmp_path):
    r = jev.ReplayJev(_Counting(), tmp_path / "cache.json")
    r.judge(STATE, QUESTIONS)
    other = {**STATE, "page": {"title": "Other"}}
    r.judge(other, QUESTIONS)
    assert r.used_keys == {jev.ReplayJev.key_for(STATE, QUESTIONS),
                          jev.ReplayJev.key_for(other, QUESTIONS)}


def test_used_keys_never_grows_on_a_miss_with_no_inner(tmp_path):
    r = jev.ReplayJev(None, tmp_path / "cache.json")
    with pytest.raises(jev.JevUnavailable):
        r.judge(STATE, QUESTIONS)
    assert r.used_keys == set()


def _seed_cache(path: Path, keys: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8")


def test_prune_cache_keeps_only_the_given_keys(tmp_path):
    cache = tmp_path / "cache.json"
    _seed_cache(cache, {"used": {"q": {"kind": "noul", "noul": 0.9}},
                        "stale": {"q": {"kind": "noul", "noul": 0.1}}})
    before, after = jev.prune_cache(cache, {"used"})
    assert (before, after) == (2, 1)
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "used": {"q": {"kind": "noul", "noul": 0.9}}}


def test_prune_cache_refuses_after_a_miss(tmp_path):
    cache = tmp_path / "cache.json"
    _seed_cache(cache, {"used": {}, "stale": {}})
    with pytest.raises(jev.PruneRefused, match="miss"):
        jev.prune_cache(cache, {"used"}, misses=1)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_cache_refuses_after_a_failure(tmp_path):
    cache = tmp_path / "cache.json"
    _seed_cache(cache, {"used": {}, "stale": {}})
    with pytest.raises(jev.PruneRefused, match="failure"):
        jev.prune_cache(cache, {"used"}, failures=1)
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used", "stale"}


def test_prune_cache_refuses_an_empty_key_set(tmp_path):
    cache = tmp_path / "cache.json"
    _seed_cache(cache, {"used": {}})
    with pytest.raises(jev.PruneRefused, match="no keys"):
        jev.prune_cache(cache, set())
    assert set(json.loads(cache.read_text(encoding="utf-8"))) == {"used"}


def test_prune_cache_writes_atomically(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    _seed_cache(cache, {"used": {}, "stale": {}})
    calls = []
    real_atomic = jev.atomic_write_json

    def _wrapped(path, data):
        calls.append((Path(path), dict(data)))
        real_atomic(path, data)
    monkeypatch.setattr(jev, "atomic_write_json", _wrapped)
    jev.prune_cache(cache, {"used"})
    assert calls == [(cache, {"used": {}})]
    assert not list(tmp_path.glob("*.tmp"))     # no stranded temp file


def test_prune_cache_treats_a_missing_cache_as_empty(tmp_path):
    cache = tmp_path / "missing.json"
    with pytest.raises(jev.PruneRefused, match="no keys"):
        jev.prune_cache(cache, set())
    before, after = jev.prune_cache(cache, {"x"})
    assert (before, after) == (0, 0)
    assert json.loads(cache.read_text(encoding="utf-8")) == {}


# --- (c) get(): the factory and the missing-key refusal ------------------------

def test_get_typesafe_without_a_key_raises_jev_unavailable_naming_the_console():
    with pytest.raises(jev.JevUnavailable) as exc:
        jev.get("typesafe")
    msg = str(exc.value)
    assert "console.typesafe.ai/keys" in msg
    assert "TYPESAFE_API_KEY" in msg and ".env" in msg
    assert "Settings > Jev" in msg          # the key row's section


def test_jev_unavailable_is_a_runtime_error():
    assert issubclass(jev.JevUnavailable, RuntimeError)


def test_get_defaults_to_typesafe_and_the_env_var_picks_the_mode(monkeypatch):
    with pytest.raises(jev.JevUnavailable):
        jev.get()
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")
    assert isinstance(jev.get(), jev.FakeJev)
    assert isinstance(jev.get(""), jev.FakeJev)


def test_get_argument_beats_the_env_var(monkeypatch):
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")
    with pytest.raises(jev.JevUnavailable):
        jev.get("typesafe")


def test_get_replay_is_read_only_when_no_key_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_APPLY_JEV_CACHE", str(tmp_path / "c.json"))
    r = jev.get("replay")
    assert isinstance(r, jev.ReplayJev)
    assert r.inner is None
    assert r.cache_path == tmp_path / "c.json"
    with pytest.raises(jev.JevUnavailable, match="cache miss"):
        r.judge(STATE, QUESTIONS)
    assert not r.cache_path.exists()


def test_get_replay_default_cache_path_sits_under_tests_fixtures():
    r = jev.get("replay")
    assert r.cache_path == REPO / "tests" / "fixtures" / "jev_cache" / "cache.json"


def test_get_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        jev.get("gemini")


# --- (d) TypeSafeJev over a mocked SDK -----------------------------------------

def _fake_sdk(monkeypatch, response, record):
    """Install a stand-in `typesafe_sdk` whose client returns `response` and
    records every constructor and system_one call into `record`."""
    class _Client:
        def __init__(self, **kw):
            record.append(("init", kw))

        def system_one(self, state, questions, **kw):
            record.append(("system_one", state, questions, kw))
            return response

    mod = types.ModuleType("typesafe_sdk")
    mod.TypeSafeClient = _Client
    mod.RetryPolicy = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", mod)


def _sdk_response(input_tokens=304):
    ns = types.SimpleNamespace
    return ns(
        model="jev-1.13.0",
        usage=ns(input_tokens=input_tokens, output_tokens=18),
        answers={
            "is_captcha": ns(type="noul", noul=0.05),
            "which_option": ns(type="choice", choice="yes", confidence=0.81,
                               probabilities={"yes": 0.88, "no": 0.12, "none": 0.0}),
            "fit": ns(type="score", score=1.05, confidence=0.92,
                      probabilities={0: 0.0, 1: 0.95, 2: 0.05},
                      legend={0: "a", 1: "b", 2: "c"}),
        },
    )


def test_typesafe_translates_nouls_choices_and_scores(monkeypatch):
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    out = jev.TypeSafeJev(api_key="k-test").judge(STATE, QUESTIONS)
    assert out["is_captcha"] == jev.Answer(kind="noul", noul=0.05)
    assert out["which_option"] == jev.Answer(
        kind="choice", choice="yes", confidence=0.81,
        probabilities={"yes": 0.88, "no": 0.12, "none": 0.0})
    assert out["fit"] == jev.Answer(
        kind="score", score=1.05, confidence=0.92,
        probabilities={"0": 0.0, "1": 0.95, "2": 0.05})


def test_typesafe_passes_raw_question_dicts_and_pins_the_model(monkeypatch):
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    jev.TypeSafeJev(api_key="k-test").judge(STATE, QUESTIONS)
    init = next(r for r in record if r[0] == "init")[1]
    assert init["api_key"] == "k-test" and init["model"] == "jev-1.13.0"
    assert init["retry"].max_retries == 0       # `jev.Guarded` owns the retries
    call = next(r for r in record if r[0] == "system_one")
    assert call[1] is STATE and call[2] is QUESTIONS
    assert call[3].get("model") == "jev-1.13.0"


def test_typesafe_adds_input_tokens_to_the_process_usage_counter(monkeypatch):
    record = []
    _fake_sdk(monkeypatch, _sdk_response(input_tokens=1_000_000), record)
    client = jev.TypeSafeJev(api_key="k-test")
    client.judge(STATE, QUESTIONS)
    client.judge(STATE, QUESTIONS)
    u = jev.usage()
    assert u["requests"] == 2 and u["input_tokens"] == 2_000_000
    assert u["usd"] == pytest.approx(2 * 0.042)
    jev.reset_usage()
    assert jev.usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}


class _SlowReads(dict):
    """A usage counter whose reads pause, so two threads that read, add and
    write it without a lock overlap and drop a count."""

    def __getitem__(self, key):
        value = super().__getitem__(key)
        time.sleep(0.0005)
        return value


def _run_threads(*targets):
    start = threading.Barrier(len(targets))

    def run(target):
        start.wait(timeout=10)
        target()

    threads = [threading.Thread(target=run, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)


def test_the_usage_counter_keeps_every_count_across_threads(monkeypatch):
    """The scorer makes its Jev requests from worker threads: every request
    counts, and usage() never reads one half counted."""
    monkeypatch.setattr(jev, "_USAGE", _SlowReads(requests=0, input_tokens=0))
    counters, per_thread, tokens = 4, 25, 10
    torn = []

    def count():
        for _ in range(per_thread):
            jev.count_usage(tokens)

    def read():
        for _ in range(per_thread):
            u = jev.usage()
            if u["input_tokens"] != tokens * u["requests"]:
                torn.append(u)

    _run_threads(*[count] * counters, read)
    assert torn == []
    u = jev.usage()
    assert u["requests"] == counters * per_thread
    assert u["input_tokens"] == counters * per_thread * tokens


def test_a_reset_never_lands_inside_a_count(monkeypatch):
    """reset_usage() between two threads' counts leaves the tokens matching
    the requests, since a reset waits for the count in progress."""
    monkeypatch.setattr(jev, "_USAGE", _SlowReads(requests=0, input_tokens=0))
    tokens = 10

    def count():
        for _ in range(40):
            jev.count_usage(tokens)

    def reset():
        for _ in range(20):
            jev.reset_usage()
            time.sleep(0.001)

    _run_threads(count, count, reset)
    u = jev.usage()
    assert u["input_tokens"] == tokens * u["requests"]


def test_the_lifetime_counter_keeps_every_count_across_threads_and_resets(monkeypatch):
    """total_usage() counts every live request the process made: counts from
    worker threads are never lost, never read half counted, and a reset of
    usage() running alongside them never touches it."""
    monkeypatch.setattr(jev, "_USAGE", _SlowReads(requests=0, input_tokens=0))
    monkeypatch.setattr(jev, "_TOTAL", _SlowReads(requests=0, input_tokens=0))
    counters, per_thread, tokens = 3, 25, 10
    torn = []

    def count():
        for _ in range(per_thread):
            jev.count_usage(tokens)

    def read():
        for _ in range(per_thread):
            t = jev.total_usage()
            if t["input_tokens"] != tokens * t["requests"]:
                torn.append(t)

    def reset():
        for _ in range(20):
            jev.reset_usage()
            time.sleep(0.001)

    _run_threads(*[count] * counters, read, reset)
    assert torn == []
    t = jev.total_usage()
    assert t == {"requests": counters * per_thread,
                 "input_tokens": counters * per_thread * tokens,
                 "usd": jev.usd_for(counters * per_thread * tokens)}


def test_reset_usage_leaves_the_lifetime_counter_alone():
    before = jev.total_usage()
    jev.count_usage(1_000_000)
    jev.count_usage(500_000)
    jev.reset_usage()
    assert jev.usage() == {"requests": 0, "input_tokens": 0, "usd": 0.0}
    after = jev.total_usage()
    assert after["requests"] - before["requests"] == 2
    assert after["input_tokens"] - before["input_tokens"] == 1_500_000
    assert after["usd"] - before["usd"] == pytest.approx(jev.usd_for(1_500_000))


def test_typesafe_reads_the_key_from_the_environment(monkeypatch):
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-env")
    assert isinstance(jev.get("typesafe"), jev.TypeSafeJev)
    assert record[0][1]["api_key"] == "k-env"


def test_get_replay_never_constructs_live_client_even_with_key(monkeypatch, tmp_path):
    calls = []
    _fake_sdk(monkeypatch, _sdk_response(), calls)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-env")
    monkeypatch.setenv("AUTO_APPLY_JEV_CACHE", str(tmp_path / "c.json"))
    r = jev.get("replay")
    assert r.inner is None
    assert calls == []
    with pytest.raises(jev.JevUnavailable, match="cache miss"):
        r.judge(STATE, QUESTIONS)
    assert calls == []


def test_read_only_replay_reads_an_explicit_recording(tmp_path):
    cache = tmp_path / "cache.json"
    expected = jev.ReplayJev(jev.FakeJev(), cache).judge(STATE, QUESTIONS)
    replay = jev.ReplayJev(None, cache)
    assert replay.judge(STATE, QUESTIONS) == expected
    assert (replay.hits, replay.misses) == (1, 0)


def test_typesafe_without_the_sdk_installed_raises_jev_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)   # import raises
    with pytest.raises(jev.JevUnavailable) as exc:
        jev.TypeSafeJev(api_key="k-test")
    # README Step 2's own command: a bare `pip` is the global interpreter's
    assert r"venv\Scripts\python.exe -m pip install -r requirements.txt" in str(exc.value)


def test_typesafe_never_calls_the_api_at_construction(monkeypatch):
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    jev.TypeSafeJev(api_key="k-test")
    assert [r[0] for r in record] == ["init"]
    assert jev.usage()["requests"] == 0


# --- (e) one layer of retries -------------------------------------------------

def test_a_judge_that_stays_down_gets_the_guards_retries_alone():
    """The installed SDK over a transport that answers every request with a
    529 (no network): `jev.Guarded` owns the retries, so the judge gets the
    first try and `RETRY_DELAYS_S`'s three more (about a minute of waits),
    never the SDK's own retries inside each one (twelve requests)."""
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("typesafe_sdk")
    sent: list[str] = []

    def _busy(request):
        sent.append(request.method)
        return httpx2.Response(529, json={"error": {"type": "overloaded_error",
                                                    "message": "overloaded"}})
    sleeps: list[float] = []
    live = jev.TypeSafeJev(api_key="k-test", transport=httpx2.MockTransport(_busy))
    judge = jev.Guarded(live, sleep=sleeps.append)
    try:
        with pytest.raises(jev.JudgeOutage):
            judge.judge(STATE, QUESTIONS)
    finally:
        live._client.close()
    assert len(sent) == 1 + len(jev.RETRY_DELAYS_S), sent
    assert sleeps == list(jev.RETRY_DELAYS_S) and sum(sleeps) <= 60
    assert judge.down.endswith(" 529")


def test_fake_takes_not_settled_whenever_it_is_listed():
    # the fake cannot read whether a saved answer settles a reworded question
    q = {"type": "choice", "instructions": {"saved_answer": "Willing to work on-site: Yes"},
         "criteria": {"Yes": None, "No": None, "not_settled": "the saved answer does not decide"}}
    a = jev.FakeJev().judge({"fields": []}, {"q": q})["q"]
    assert (a.choice, a.confidence) == (jev.NOT_SETTLED, 1.0)
    assert a.probabilities == {"Yes": 0.0, "No": 0.0, "not_settled": 1.0}


# --- a choice's options ---------------------------------------------------------------

def _choice(n: int) -> dict:
    return {"type": "choice", "instructions": "Which option?",
            "criteria": {f"o{i}": None for i in range(n)}}


def test_the_choice_option_limit_is_jevs():
    assert jev.CHOICE_OPTIONS_MAX == 255


def test_a_choice_past_jevs_option_limit_does_not_fit():
    """The service answers 400 to a choice of about 360 verbs in a request well
    under the token limits, so only the option count shows it."""
    state = {"bullet": "Built a sales model."}
    assert jev.request_fits(state, {"q": _choice(jev.CHOICE_OPTIONS_MAX)})
    assert not jev.request_fits(state, {"q": _choice(jev.CHOICE_OPTIONS_MAX + 1)})
    assert not jev.request_fits(state, {"a": _choice(2),
                                        "q": _choice(jev.CHOICE_OPTIONS_MAX + 1)})
    _longest, whole = jev.request_size(state, {"q": _choice(jev.CHOICE_OPTIONS_MAX + 1)})
    assert whole < jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN


def test_a_noul_or_a_score_has_no_options_to_count():
    state = {"bullet": "Built a sales model."}
    noul = {"type": "noul", "instructions": "Is it?", "criteria": {"true": "yes", "false": "no"}}
    score = {"type": "score", "instructions": "How much?", "criteria": ["none", "some", "all"]}
    assert jev.request_fits(state, {"n": noul, "s": score})


class _Counting:
    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        return jev.FakeJev().judge(state, questions)


def test_the_guard_refuses_a_choice_past_the_option_limit_before_sending(caplog):
    """The service answers such a request 400 every time, so the guard refuses it
    unsent, the way a 400 passes through it: no retry, and the breaker stays shut."""
    inner, sleeps = _Counting(), []
    judge = jev.Guarded(inner, sleep=sleeps.append, logger=logging.getLogger("test_jev.options"))
    with caplog.at_level(logging.WARNING, logger="test_jev.options"):
        with pytest.raises(jev.RequestRejected) as err:
            judge.judge({"bullet": "Built"}, {"verb": _choice(jev.CHOICE_OPTIONS_MAX + 1)})
    assert str(err.value) == "jev choice verb has 256 options, past the 255 a choice takes"
    assert isinstance(err.value, ValueError) and not isinstance(err.value, jev.JudgeOutage)
    assert jev.error_kind(err.value) == "RequestRejected"
    assert inner.calls == 0 and sleeps == []
    assert (judge.down, judge.refused, judge.request_fault, judge.answers) == ("", False, False, 0)
    assert [r.getMessage() for r in caplog.records] == [
        "jev choice verb has 256 options, past the 255 a choice takes"]
    # The next request that fits goes out as ever.
    got = judge.judge({"bullet": "Built"}, {"verb": _choice(jev.CHOICE_OPTIONS_MAX)})
    assert got["verb"].kind == "choice" and inner.calls == 1 and judge.answers == 1


def test_a_the_typesafe_client_pins_its_host_over_the_env(monkeypatch):
    """4-A LOW: a TYPESAFE_BASE_URL in the environment (or a .env row) would
    send the key and every page summary to another host. The client names the
    real API root itself."""
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://collector.example.test")
    record = []
    _fake_sdk(monkeypatch, _sdk_response(), record)
    jev.TypeSafeJev(api_key="k-test")
    init = next(r for r in record if r[0] == "init")[1]
    assert init["base_url"] == "https://api.typesafe.ai" == jev.BASE_URL
