import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import keypool  # noqa: E402


def test_key_fingerprint_is_stable_8char_and_not_raw():
    fp = keypool.key_fingerprint("AQ.supersecretvalue")
    assert len(fp) == 8
    assert "secret" not in fp
    assert fp == keypool.key_fingerprint("AQ.supersecretvalue")
    assert fp != keypool.key_fingerprint("AQ.different")


def test_pacific_today_is_iso_date():
    d = keypool.pacific_today()
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"


def test_usage_state_incr_and_persist(tmp_path):
    p = tmp_path / "score_state.json"
    st = keypool.UsageState(p)
    st.load()
    st.incr("fp1", "gemini-3.5-flash", 3)
    st.set_exhausted("fp2", "gemini-3.1-flash-lite", 500)
    st.save()

    st2 = keypool.UsageState(p)
    st2.load()
    assert st2.get("fp1", "gemini-3.5-flash") == 3
    assert st2.get("fp2", "gemini-3.1-flash-lite") == 500
    # fingerprints only -- no raw key material on disk
    assert "supersecret" not in p.read_text(encoding="utf-8")


def test_usage_state_resets_on_date_rollover(tmp_path):
    p = tmp_path / "score_state.json"
    p.write_text(json.dumps({"date": "2000-01-01", "usage": {"fp1:m": 9}}), encoding="utf-8")
    st = keypool.UsageState(p)
    st.load()
    assert st.get("fp1", "m") == 0


def _resp(tag):
    return SimpleNamespace(text=tag, usage_metadata=SimpleNamespace(
        prompt_token_count=1, candidates_token_count=1))


def _client(responder):
    async def gen(*, model, contents, config):
        return responder(model, contents, config)
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))


def _pool(members, tmp_path, limits=None):
    st = keypool.UsageState(tmp_path / "s.json")
    st.load()
    return keypool.KeyPool(members, st, limits=limits)


FLASH = "gemini-3.5-flash"


def test_generate_uses_free_key_then_counts_it(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "FREE"
    assert pool.stats() == {"free_calls": 1, "vertex_calls": 0}


def test_generate_spills_to_vertex_when_free_rpd_exhausted(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex", "fp": None}
    pool = _pool([free, vertex], tmp_path)
    pool._state.set_exhausted("fp1", FLASH, keypool.LIMITS[FLASH]["rpd"])
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "VERTEX"
    assert pool.stats() == {"free_calls": 0, "vertex_calls": 1}


def test_generate_parks_the_pair_on_an_unproven_429_then_fails_over(tmp_path):
    # The 429 says only "you exceeded your current quota" -- it does not say
    # which one. Failing over is right; writing off the day is not.
    def boom(*_):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    free = {"client": _client(boom), "kind": "free", "fp": "fp1"}
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex", "fp": None}
    pool = _pool([free, vertex], tmp_path)
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "VERTEX"
    assert pool._state.get("fp1", FLASH) < keypool.LIMITS[FLASH]["rpd"]
    assert pool._pair_cooling_left(("fp1", FLASH), time.monotonic()) > 0


def test_generate_raises_pool_error_when_no_member(tmp_path):
    pool = _pool([], tmp_path)
    try:
        asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
        assert False, "expected PoolError"
    except keypool.PoolError:
        pass


def test_select_waits_when_free_key_rpm_throttled(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex", "fp": None}
    pool = _pool([free, vertex], tmp_path)
    # Fill the free key's RPM window to the limit -> throttled but RPD still left.
    now = time.monotonic()
    pool._rpm[(0, FLASH)].extend([now] * keypool.LIMITS[FLASH]["rpm"])
    kind, idx, _model, wait = pool._select([FLASH], {FLASH: keypool.LIMITS[FLASH]})
    assert kind == "wait" and wait > 0


def test_from_env_builds_free_members_and_vertex(monkeypatch, tmp_path):
    created = []

    def fake_client(**kwargs):
        created.append(kwargs)
        return _client(lambda *_: _resp("ok"))

    monkeypatch.setattr("google.genai.Client", fake_client)
    monkeypatch.setenv("GEMINI_API_KEYS", "k1, k2 ,k3")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    pool = keypool.KeyPool.from_env(state_path=tmp_path / "s.json")
    kinds = [m["kind"] for m in pool._members]
    assert kinds == ["free", "free", "free", "vertex"]
    assert pool._members[-1]["fp"] is None
    assert all(m["fp"] for m in pool._members[:3])
    assert created[0]["api_key"] == "k1"
    assert created[-1]["vertexai"] is True
    assert created[-1]["project"] == "proj"
    assert created[-1]["location"] == "global"
    # every client (free and vertex) carries a bounded HTTP timeout (P1-4)
    assert all("http_options" in kwargs for kwargs in created)


def test_from_env_raises_without_any_credential(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr("google.genai.Client", lambda **k: None)
    try:
        keypool.KeyPool.from_env(state_path=tmp_path / "s.json")
        assert False, "expected PoolError"
    except keypool.PoolError:
        pass


UNKNOWN_MODEL = "gemini-3.1-pro-preview"


# --- P0-2: unknown models must get DEFAULT_LIMITS RPM/RPD gating -----------

def test_default_limits_exist_and_are_conservative():
    assert keypool.DEFAULT_LIMITS["rpm"] > 0
    assert keypool.DEFAULT_LIMITS["rpd"] > 0


def test_select_gates_unknown_model_by_default_limits_once_rpd_exhausted(tmp_path):
    # No vertex member -- if an unknown model isn't gated, _select would keep
    # handing out the free key forever (the P0-2 infinite-loop bug).
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)
    limits = keypool.LIMITS.get(UNKNOWN_MODEL, keypool.DEFAULT_LIMITS)
    pool._state.set_exhausted("fp1", UNKNOWN_MODEL, limits["rpd"])
    kind, idx, _model, wait = pool._select([UNKNOWN_MODEL], {UNKNOWN_MODEL: limits})
    assert kind == "none"


def test_generate_raises_pool_error_for_unknown_model_when_free_rpd_exhausted(tmp_path):
    # Mirrors the real generate() codepath: LIMITS.get(model, DEFAULT_LIMITS)
    # must be what generate() actually uses, not None.
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)
    limits = keypool.LIMITS.get(UNKNOWN_MODEL, keypool.DEFAULT_LIMITS)
    pool._state.set_exhausted("fp1", UNKNOWN_MODEL, limits["rpd"])
    try:
        asyncio.run(pool.generate(model=UNKNOWN_MODEL, contents="x", config=None))
        assert False, "expected PoolError (unknown model must be gated, not unthrottled)"
    except keypool.PoolError:
        pass


def test_known_model_limits_unchanged_by_default_limits_addition():
    # No happy-path change for the two known models.
    assert keypool.LIMITS[FLASH] == {"rpm": 5, "rpd": 20}
    assert keypool.LIMITS["gemini-3.1-flash-lite"] == {"rpm": 15, "rpd": 500}


# --- P1-4: from_env-built clients must carry an HTTP timeout ---------------

def test_from_env_sets_default_http_timeout_on_free_and_vertex_clients(monkeypatch, tmp_path):
    created = []

    def fake_client(**kwargs):
        created.append(kwargs)
        return _client(lambda *_: _resp("ok"))

    monkeypatch.setattr("google.genai.Client", fake_client)
    monkeypatch.delenv("SCORE_HTTP_TIMEOUT_S", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    keypool.KeyPool.from_env(state_path=tmp_path / "s.json")

    assert len(created) == 2
    for kwargs in created:
        assert "http_options" in kwargs
        assert kwargs["http_options"].timeout == 120000


def test_from_env_respects_score_http_timeout_s_env_override(monkeypatch, tmp_path):
    created = []

    def fake_client(**kwargs):
        created.append(kwargs)
        return _client(lambda *_: _resp("ok"))

    monkeypatch.setattr("google.genai.Client", fake_client)
    monkeypatch.setenv("SCORE_HTTP_TIMEOUT_S", "45")
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    keypool.KeyPool.from_env(state_path=tmp_path / "s.json")

    assert len(created) == 2
    for kwargs in created:
        assert kwargs["http_options"].timeout == 45000


# --- P2-8: UsageState robustness --------------------------------------------

def test_usage_state_load_survives_non_int_usage_value(tmp_path):
    p = tmp_path / "score_state.json"
    today = keypool.pacific_today()
    p.write_text(json.dumps({"date": today, "usage": {"fp1:m": "garbage"}}), encoding="utf-8")
    st = keypool.UsageState(p)
    st.load()  # must not raise
    assert st.get("fp1", "m") == 0


def test_usage_state_load_survives_mixed_valid_and_invalid_values(tmp_path):
    p = tmp_path / "score_state.json"
    today = keypool.pacific_today()
    p.write_text(
        json.dumps({"date": today, "usage": {"fp1:m": 7, "fp2:m": "garbage", "fp3:m": None}}),
        encoding="utf-8",
    )
    st = keypool.UsageState(p)
    st.load()  # must not raise
    assert st.get("fp1", "m") == 7
    assert st.get("fp2", "m") == 0
    assert st.get("fp3", "m") == 0


def test_usage_state_save_writes_atomically_via_os_replace(tmp_path, monkeypatch):
    p = tmp_path / "score_state.json"
    st = keypool.UsageState(p)
    st.load()
    st.incr("fp1", "m", 5)

    calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(keypool.os, "replace", spy_replace)
    st.save()

    assert len(calls) == 1
    assert calls[0][1] == str(p)
    # temp file was in the same directory (same-filesystem rename guarantee)
    assert Path(calls[0][0]).parent == p.parent
    assert not Path(calls[0][0]).exists()  # renamed away, no leftover temp file

    st2 = keypool.UsageState(p)
    st2.load()
    assert st2.get("fp1", "m") == 5


def test_usage_state_save_content_round_trips(tmp_path):
    p = tmp_path / "score_state.json"
    st = keypool.UsageState(p)
    st.load()
    st.incr("fpA", "gemini-3.5-flash", 2)
    st.set_exhausted("fpB", "gemini-3.1-flash-lite", 500)
    st.save()

    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["date"] == st.date
    assert on_disk["usage"]["fpA:gemini-3.5-flash"] == 2
    assert on_disk["usage"]["fpB:gemini-3.1-flash-lite"] == 500


# --- P2-4: RPD state must roll over at Pacific midnight mid-process ----------

def test_select_frees_exhausted_key_after_pacific_midnight(tmp_path, monkeypatch):
    # A long-running process that crosses midnight Pacific: a key exhausted on
    # day D must become selectable on D+1 (real Gemini quota reset), without
    # restarting the process.
    monkeypatch.setattr(keypool, "pacific_today", lambda: "2020-01-01")
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)  # state.date pinned to day D
    assert pool._state.date == "2020-01-01"
    pool._state.set_exhausted("fp1", FLASH, keypool.LIMITS[FLASH]["rpd"])
    pool._state.save()
    # Day D: no free RPD headroom, no vertex backstop -> nothing usable.
    assert pool._select([FLASH], {FLASH: keypool.LIMITS[FLASH]})[0] == "none"

    # Cross midnight into D+1.
    monkeypatch.setattr(keypool, "pacific_today", lambda: "2020-01-02")
    kind, idx, _model, _ = pool._select([FLASH], {FLASH: keypool.LIMITS[FLASH]})
    assert kind == "free" and idx == 0
    # State rolled over: new day, exhausted usage cleared.
    assert pool._state.date == "2020-01-02"
    assert pool._state.get("fp1", FLASH) == 0


def test_generate_rolls_over_and_attributes_usage_to_new_day(tmp_path, monkeypatch):
    monkeypatch.setattr(keypool, "pacific_today", lambda: "2020-01-01")
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)
    pool._state.set_exhausted("fp1", FLASH, keypool.LIMITS[FLASH]["rpd"])
    pool._state.save()
    # Day D with the only free key exhausted and no vertex -> PoolError.
    try:
        asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
        assert False, "expected PoolError while exhausted on day D"
    except keypool.PoolError:
        pass

    # New day: the same key is used again, and usage is attributed to D+1.
    monkeypatch.setattr(keypool, "pacific_today", lambda: "2020-01-02")
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "FREE"
    assert pool.stats() == {"free_calls": 1, "vertex_calls": 0}
    pool._state.save()   # P2-23: saves are debounced; flush before reading disk
    on_disk = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert on_disk["date"] == "2020-01-02"
    assert on_disk["usage"]["fp1:" + FLASH] == 1


def test_vertex_quota_pool_error_chains_the_quota_exception(tmp_path, monkeypatch):
    # After the bounded retries, the PoolError must carry the underlying quota
    # error as its explicit __cause__ (raise ... from exc), not just implicit
    # context -- the root cause must survive into logs.
    async def no_sleep(_):
        return None
    monkeypatch.setattr(keypool.asyncio, "sleep", no_sleep)

    def boom(*_):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    vertex = {"client": _client(boom), "kind": "vertex", "fp": None}
    pool = _pool([vertex], tmp_path)
    try:
        asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
        assert False, "expected PoolError"
    except keypool.PoolError as e:
        assert "quota" in str(e).lower()
        assert isinstance(e.__cause__, RuntimeError)


# -- audit P2-1: separate quota vs transient retry budgets ---------------------

def test_mixed_quota_and_transient_errors_use_separate_budgets(tmp_path, monkeypatch):
    """Two vertex 429s then two generic transient errors must still succeed on
    the fifth call: the old SHARED counter hit the transient threshold (3) after
    429+429+error and gave up with only one generic failure observed."""
    calls = {"n": 0}

    def responder(*_):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
        if calls["n"] <= 4:
            raise RuntimeError("503 transient hiccup")
        return _resp("OK")

    vertex = {"client": _client(responder), "kind": "vertex", "fp": None}
    pool = _pool([vertex], tmp_path)

    async def no_sleep(_secs):
        return None

    monkeypatch.setattr(keypool.asyncio, "sleep", no_sleep)
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "OK" and calls["n"] == 5


# -- audit P2-23: debounced saves + concurrent-process merge -------------------

def test_usage_state_maybe_save_debounces(tmp_path):
    p = tmp_path / "s.json"
    st = keypool.UsageState(p)
    st.load()
    for _ in range(keypool.UsageState._SAVE_EVERY - 1):
        st.incr("fp1", "m")
        st.maybe_save()
    assert not p.exists()              # under the threshold: no write yet
    st.incr("fp1", "m")
    st.maybe_save()
    assert p.exists()                  # threshold reached: flushed


def test_save_merges_concurrent_process_counters(tmp_path):
    """Two processes sharing score_state.json must not last-writer-wins each
    other's RPD counts (undercounting risks avoidable 429s)."""
    p = tmp_path / "s.json"
    a = keypool.UsageState(p)
    a.load()
    a.incr("fp1", "m", 5)
    a.save()
    b = keypool.UsageState(p)
    b.load()                           # b sees fp1=5
    a.incr("fp1", "m", 3)              # a: fp1=8 (in memory)
    b.incr("fp2", "m", 2)
    b.save()                           # disk: fp1=5, fp2=2
    a.save()                           # merge: must keep fp2 AND a's higher fp1
    c = keypool.UsageState(p)
    c.load()
    assert c.get("fp1", "m") == 8
    assert c.get("fp2", "m") == 2


def test_atexit_flush_registered_once_per_state(tmp_path, monkeypatch):
    """audit C6-8: the dashboard builds a fresh KeyPool per scoring run inside one
    long-lived process, and __init__ called atexit.register unconditionally — so
    handlers accumulated for the life of the app and every one of them wrote the
    same state file at shutdown."""
    import atexit as _atexit

    registered = []
    monkeypatch.setattr(_atexit, "register", lambda fn, *a, **k: registered.append(fn) or fn)

    state = keypool.UsageState(tmp_path / "usage.json")
    for _ in range(5):
        keypool.KeyPool([], state)
    assert len(registered) == 1, f"{len(registered)} atexit handlers for one state"

    # a genuinely different state still gets its own flush
    keypool.KeyPool([], keypool.UsageState(tmp_path / "other.json"))
    assert len(registered) == 2


# --- configured free-tier limits: the dashboard's numbers beat the built-in table --
#
# The 2026-09-08 incident this guards: scoring_config.json had migrated to
# gemini-3.5-flash-lite / gemini-3.8-flash, neither of which is a LIMITS key, so
# BOTH silently fell to DEFAULT_LIMITS {rpm 5, rpd 100}. Stage 1 ran at a third of
# its real RPM, and stage 2 pinned all three keys at exactly 100 and spilled every
# further call onto the paid Vertex backstop. Nothing logged; the run just crawled.
#
# A model id is a moving target, so the fix is not "add two more rows" -- it is
# that the numbers travel with the model choice, from the same Settings tab.

CONFIGURED_MODEL = "gemini-9.9-flash"   # a plausible id absent from LIMITS, as of today


def test_limits_for_prefers_the_configured_override(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path, limits={CONFIGURED_MODEL: {"rpm": 12, "rpd": 400}})
    assert pool._limits_for(CONFIGURED_MODEL) == {"rpm": 12, "rpd": 400}


def test_limits_for_falls_back_to_builtin_table_then_default(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)          # nothing configured
    assert pool._limits_for(FLASH) == keypool.LIMITS[FLASH]
    assert pool._limits_for(UNKNOWN_MODEL) == keypool.DEFAULT_LIMITS


def test_partial_override_merges_rather_than_silently_dropping_the_set_half(tmp_path):
    # Half-filled boxes are the norm (AI Studio shows RPM more prominently than
    # RPD). Requiring both would silently ignore the number the user DID type --
    # the exact failure class this whole change exists to remove.
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path, limits={FLASH: {"rpm": 30, "rpd": 0}})
    assert pool._limits_for(FLASH) == {"rpm": 30, "rpd": keypool.LIMITS[FLASH]["rpd"]}


def test_override_lifts_a_model_past_the_phantom_default_rpd_ceiling(tmp_path):
    # The incident, reproduced: at DEFAULT_LIMITS the key is exhausted at 100 and
    # _select abandons it. With the real free-tier rpd configured it keeps serving.
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path, limits={CONFIGURED_MODEL: {"rpm": 15, "rpd": 500}})
    pool._state.incr("fp1", CONFIGURED_MODEL, keypool.DEFAULT_LIMITS["rpd"])

    ungated = _pool([free], tmp_path)       # same usage, no configured limits
    ungated._state.incr("fp1", CONFIGURED_MODEL, keypool.DEFAULT_LIMITS["rpd"])

    assert pool._select([CONFIGURED_MODEL],
                        pool._limits_by_model([CONFIGURED_MODEL]))[0] == "free"
    assert ungated._select([CONFIGURED_MODEL],
                           ungated._limits_by_model([CONFIGURED_MODEL]))[0] == "none"


def test_generate_honours_the_configured_limits_not_the_table(tmp_path):
    # generate() must resolve through _limits_for; a stray LIMITS.get() here would
    # reinstate the bug while every _select test above still passed.
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex", "fp": None}
    pool = _pool([free, vertex], tmp_path,
                 limits={CONFIGURED_MODEL: {"rpm": 15, "rpd": 500}})
    pool._state.incr("fp1", CONFIGURED_MODEL, keypool.DEFAULT_LIMITS["rpd"])
    resp = asyncio.run(pool.generate(model=CONFIGURED_MODEL, contents="x", config=None))
    assert resp.text == "FREE"          # "VERTEX" == still gated at rpd 100
    assert pool.stats()["vertex_calls"] == 0


# ---------------------------------------------------------------------------
# Synchronous lease lane (the resume tailor's llm.py drives its own request and
# retry envelope, so it leases a member instead of handing the call to the pool).
# ---------------------------------------------------------------------------
def _sync_member(fp, kind="free"):
    """A member whose client is never called -- lease_sync only selects."""
    return {"client": SimpleNamespace(), "kind": kind, "fp": fp}


def test_lease_sync_returns_a_free_member_and_counts_it(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path)
    member, _model = pool.lease_sync(FLASH)
    assert member["fp"] == "fp1"
    assert pool._state.get("fp1", FLASH) == 1
    assert pool.stats() == {"free_calls": 1, "vertex_calls": 0}


def test_lease_sync_rotates_to_the_next_key_when_the_first_is_rpm_full(tmp_path):
    pool = _pool([_sync_member("fp1"), _sync_member("fp2")], tmp_path,
                 limits={FLASH: {"rpm": 1, "rpd": 50}})
    first, _m1 = pool.lease_sync(FLASH)
    second, _m2 = pool.lease_sync(FLASH)
    assert {first["fp"], second["fp"]} == {"fp1", "fp2"}, "must spread across keys"


def test_lease_sync_spills_to_vertex_once_every_free_key_is_rpd_exhausted(tmp_path):
    free = _sync_member("fp1")
    vertex = _sync_member(None, kind="vertex")
    pool = _pool([free, vertex], tmp_path)
    pool._state.set_exhausted("fp1", FLASH, keypool.LIMITS[FLASH]["rpd"])
    member, _model = pool.lease_sync(FLASH)
    assert member["kind"] == "vertex"
    assert pool.stats() == {"free_calls": 0, "vertex_calls": 1}


def test_lease_sync_gates_on_configured_limits_not_the_default_ceiling(tmp_path):
    # A model with no LIMITS row: without the override it would gate at
    # DEFAULT_LIMITS["rpd"] (100) and spill to Vertex 400 calls early.
    model = CONFIGURED_MODEL
    assert model not in keypool.LIMITS
    free = _sync_member("fp1")
    vertex = _sync_member(None, kind="vertex")
    pool = _pool([free, vertex], tmp_path,
                 limits={model: {"rpm": 10000, "rpd": 500}})
    pool._state.incr("fp1", model, keypool.DEFAULT_LIMITS["rpd"])
    assert pool.lease_sync(model)[0]["kind"] == "free", \
        "configured rpd 500 must outrank the 100 default"


def test_lease_sync_falls_to_vertex_rather_than_waiting_past_max_wait(tmp_path):
    free = _sync_member("fp1")
    vertex = _sync_member(None, kind="vertex")
    pool = _pool([free, vertex], tmp_path, limits={FLASH: {"rpm": 1, "rpd": 50}})
    assert pool.lease_sync(FLASH)[0]["kind"] == "free"   # burns the 1 rpm slot
    started = time.monotonic()
    member, _model = pool.lease_sync(FLASH, max_wait=0.0)
    assert member["kind"] == "vertex"
    assert time.monotonic() - started < 5.0, "must not sit out the rpm window"


def test_lease_sync_raises_pool_error_when_nothing_is_usable(tmp_path):
    pool = _pool([], tmp_path)
    try:
        pool.lease_sync(FLASH)
        assert False, "expected PoolError"
    except keypool.PoolError:
        pass


def test_mark_exhausted_stamps_the_configured_rpd_and_ignores_vertex(tmp_path):
    model = "gemini-3.8-flash"
    free = _sync_member("fp1")
    vertex = _sync_member(None, kind="vertex")
    pool = _pool([free, vertex], tmp_path, limits={model: {"rpm": 5, "rpd": 20}})
    pool.mark_exhausted(free, model)
    assert pool._state.get("fp1", model) == 20
    pool.mark_exhausted(vertex, model)      # must not blow up on fp=None
    assert pool._state.get(None, model) == 0


def test_lease_sync_does_not_over_reserve_under_concurrent_threads(tmp_path):
    import threading
    free = [_sync_member("fp1"), _sync_member("fp2")]
    vertex = _sync_member(None, kind="vertex")
    pool = _pool(free + [vertex], tmp_path,
                 limits={FLASH: {"rpm": 10000, "rpd": 5}})
    kinds: list[str] = []
    lock = threading.Lock()

    def worker():
        m, _model = pool.lease_sync(FLASH, max_wait=0.0)
        with lock:
            kinds.append(m["kind"])

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 2 keys x rpd 5 = exactly 10 free leases; the rest must land on Vertex.
    assert kinds.count("free") == 10
    assert kinds.count("vertex") == 10
    assert pool._state.get("fp1", FLASH) + pool._state.get("fp2", FLASH) == 10


# ---------------------------------------------------------------------------
# limits_from_config / limits_from_disk: the shared model -> rpm/rpd mapping,
# read by the scorer (its own config dict) and the tailor (straight off disk).
# ---------------------------------------------------------------------------
def test_limits_from_config_reads_the_per_model_rows():
    out = keypool.limits_from_config({
        "model_limits": ["m1 15 500", "m2 5 20"]})
    assert out == {"m1": {"rpm": 15, "rpd": 500}, "m2": {"rpm": 5, "rpd": 20}}


def test_limits_from_config_is_empty_without_rows():
    # Empty is the NORMAL case: keypool.LIMITS already covers every model the
    # Settings dropdown offers, so a row is only for one it does not know.
    assert keypool.limits_from_config({}) == {}
    assert keypool.limits_from_config({"model_limits": []}) == {}


def test_one_model_named_by_both_stages_cannot_collapse():
    """The regression the per-stage rpm/rpd boxes caused and this replaced.

    Two stages pointing at gemini-3.5-flash-lite left it gated at stage 2's
    5 rpm / 20 rpd instead of its real 15 / 500 -- 96% of the model's free quota
    silently redirected to the paid backstop. Rows are keyed by model, so there
    is no second stage to overwrite the first.
    """
    out = keypool.limits_from_config({
        "stage1_model": "shared", "stage2_model": "shared",
        "model_limits": ["shared 15 500"]})
    assert out == {"shared": {"rpm": 15, "rpd": 500}}


def test_limits_from_disk_reads_the_scoring_config_file(tmp_path):
    (tmp_path / "scoring_config.json").write_text(json.dumps({
        "model_limits": ["m1 15 500", "m2 5 20"]}), encoding="utf-8")
    assert keypool.limits_from_disk(tmp_path) == {
        "m1": {"rpm": 15, "rpd": 500}, "m2": {"rpm": 5, "rpd": 20}}


def test_limits_from_disk_survives_a_missing_or_corrupt_file(tmp_path):
    assert keypool.limits_from_disk(tmp_path) == {}
    (tmp_path / "scoring_config.json").write_text("{not json", encoding="utf-8")
    assert keypool.limits_from_disk(tmp_path) == {}


def test_limits_from_disk_lets_the_environment_win_over_the_file(tmp_path, monkeypatch):
    (tmp_path / "scoring_config.json").write_text(json.dumps({
        "model_limits": ["m1 15 500"]}), encoding="utf-8")
    monkeypatch.setenv("SCORE_MODEL_LIMITS", "m1 9 777")
    assert keypool.limits_from_disk(tmp_path) == {"m1": {"rpm": 9, "rpd": 777}}


# ── ranked multi-model selection (lateral spill) ─────────────────────────────
# The stage names several interchangeable models. Free-tier quota is metered per
# (key, model), so the ranked list multiplies the day's allowance instead of
# sharing one. Order is preference; the spill sideways is what buys the RPM.

M1 = "gemini-3.8-flash"     # ranked first
M2 = "gemini-3.7-flash"
M3 = "gemini-3.6-flash"
RANKED = [M1, M2, M3]


def test_as_model_list_splits_and_dedupes():
    assert keypool.as_model_list("a, b;c  d") == ["a", "b", "c", "d"]
    assert keypool.as_model_list(["a", "b", "a"]) == ["a", "b"]
    assert keypool.as_model_list(None) == []
    assert keypool.as_model_list("  ") == []


def test_ranked_models_keeps_the_primary_first():
    assert keypool.ranked_models(M1, [M2, M1, M3]) == [M1, M2, M3]
    assert keypool.ranked_models(M1) == [M1]
    assert keypool.ranked_models("", [M2]) == [M2]


def test_parse_model_limits_accepts_three_separators_and_skips_junk():
    rows = [f"{M1} 5 20", f"{M2},15,500", f"{M3}:1:2", "nonsense", f"{M1} 5"]
    assert keypool.parse_model_limits(rows) == {
        M1: {"rpm": 5, "rpd": 20},          # the 2-field row is skipped, not fatal
        M2: {"rpm": 15, "rpd": 500},
        M3: {"rpm": 1, "rpd": 2},
    }


def test_model_limits_beat_the_built_in_table():
    # The escape hatch for a model whose built-in numbers are wrong or absent.
    pool = _pool([_sync_member("fp1")], Path(tempfile.mkdtemp()),
                 limits=keypool.limits_from_config({"model_limits": [f"{M1} 15 500"]}))
    assert pool._limits_for(M1) == {"rpm": 15, "rpd": 500}
    assert keypool.LIMITS[M1] == {"rpm": 5, "rpd": 20}      # table says otherwise


def test_select_prefers_the_first_model_while_it_has_room(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    kind, idx, model, _ = pool._select(RANKED, pool._limits_by_model(RANKED))
    assert (kind, idx, model) == ("free", 0, M1)


def test_select_spills_sideways_when_the_top_model_is_rpm_full(tmp_path):
    # THE feature: M1 has 19 of 20 RPD left on every key but its 60s window is
    # full. Waiting would idle for up to a minute; M2 is an independent
    # allowance, so the call goes there instead.
    members = [_sync_member("fp1"), _sync_member("fp2")]
    pool = _pool(members, tmp_path, limits={m: {"rpm": 1, "rpd": 20} for m in RANKED})
    for _ in members:
        assert pool.lease_sync(RANKED)[1] == M1        # burns both M1 rpm slots
    member, model = pool.lease_sync(RANKED, max_wait=0.0)
    assert (member["kind"], model) == ("free", M2), "must move sideways, not wait"
    assert pool._state.get("fp1", M1) == 1 and pool._state.get("fp1", M2) == 1


def test_select_falls_to_the_next_model_when_the_top_is_rpd_exhausted(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    pool._state.incr("fp1", M1, 20)
    pool._state.incr("fp1", M2, 20)
    assert pool.lease_sync(RANKED)[1] == M3


def test_select_waits_only_when_no_model_anywhere_is_usable(tmp_path):
    # Every (key, model) pair has RPD headroom but a full RPM window: there is
    # nowhere to spill sideways, so waiting is correct.
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 1, "rpd": 20} for m in RANKED})
    for model in RANKED:
        assert pool.lease_sync(RANKED)[1] == model
    kind, _idx, _model, wait = pool._select(RANKED, pool._limits_by_model(RANKED))
    assert kind == "wait" and 0 < wait <= 60.0


def test_vertex_is_the_last_resort_and_keeps_the_preferred_model(tmp_path):
    members = [_sync_member("fp1"), _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    for model in RANKED:
        pool._state.incr("fp1", model, 20)
    member, model = pool.lease_sync(RANKED)
    assert member["kind"] == "vertex"
    assert model == M1, "paid path is unmetered here -- no reason to degrade too"
    assert pool.stats() == {"free_calls": 0, "vertex_calls": 1}


def test_ranked_list_multiplies_the_daily_allowance(tmp_path):
    # The whole point, in numbers: 2 keys x 3 models x rpd 2 = 12 free calls,
    # where a single model would have given 4 before spilling onto paid Vertex.
    members = [_sync_member("fp1"), _sync_member("fp2"),
               _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path,
                 limits={m: {"rpm": 1000, "rpd": 2} for m in RANKED})
    kinds = [pool.lease_sync(RANKED)[0]["kind"] for _ in range(13)]
    assert kinds.count("free") == 12
    assert kinds[-1] == "vertex"


def test_generate_retires_only_the_failing_pair_then_uses_the_next_model(tmp_path):
    # A 429 on (key, M1) must not cost that key its M2 allowance.
    seen = []

    def responder(model, *_):
        seen.append(model)
        if model == M1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
        return _resp("OK")

    free = {"client": _client(responder), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    resp = asyncio.run(pool.generate(model=RANKED, contents="x", config=None))
    assert resp.text == "OK"
    assert seen == [M1, M2]
    assert pool._state.get("fp1", M1) == 1       # parked, NOT written off
    assert pool._state.get("fp1", M2) == 1       # and only that pair moved on


def test_generate_accepts_a_bare_model_string_unchanged(tmp_path):
    free = {"client": _client(lambda *_: _resp("FREE")), "kind": "free", "fp": "fp1"}
    pool = _pool([free], tmp_path)
    assert asyncio.run(pool.generate(model=FLASH, contents="x", config=None)).text == "FREE"


def test_empty_model_list_is_a_pool_error(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path)
    for bad in ("", [], "  "):
        try:
            pool.lease_sync(bad)
        except keypool.PoolError:
            continue
        raise AssertionError(f"{bad!r} should raise PoolError")


def test_every_gemini_models_choice_has_a_real_limits_row():
    # A fallback model is picked in Settings and never gets its own rpm/rpd
    # boxes, so a missing row here silently gates it at DEFAULT_LIMITS and
    # spills the overflow onto paid Vertex. Only the pro preview is exempt.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
    import settings
    missing = [m for m in settings.GEMINI_MODELS
               if m not in keypool.LIMITS and "pro" not in m]
    assert not missing, f"no keypool.LIMITS row for {missing}"


# -- 503 / model overload -----------------------------------------------------
# A quota error means "this (key, model) pair has spent its day". A 503 means
# "Google is momentarily short of capacity for this MODEL" -- minutes, not a
# day, and true for every key at once. Retiring the pair would throw away a
# whole allowance; retrying the same model would just collect another 503. The
# pool parks the model for a cooldown and spills sideways, exactly as it does
# for a full RPM window.
class _Overload(Exception):
    """The shape google-genai raises for a 503 (APIError sets .code/.status)."""

    code = 503
    status = "UNAVAILABLE"

    def __str__(self):
        return ("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model "
                "is currently experiencing high demand. Spikes in demand are "
                "usually temporary. Please try again later.', "
                "'status': 'UNAVAILABLE'}}")


def test_is_overload_error_reads_the_sdk_fields_not_the_prose():
    assert keypool._is_overload_error(_Overload())
    assert keypool._is_overload_error(SimpleNamespace(code=503))
    assert keypool._is_overload_error(RuntimeError("503 UNAVAILABLE. overloaded"))


def test_a_quota_error_is_not_read_as_an_overload():
    quota = RuntimeError("429 RESOURCE_EXHAUSTED. quota exceeded")
    assert keypool._is_quota_error(quota)
    assert not keypool._is_overload_error(quota)
    # ...and the reverse: an overload must never be charged as spent quota.
    assert not keypool._is_quota_error(_Overload())


def test_select_skips_a_model_cooling_down_after_a_503(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    pool.mark_unavailable(M1)
    kind, _idx, model, _ = pool._select(RANKED, pool._limits_by_model(RANKED))
    assert (kind, model) == ("free", M2)


def test_the_cooldown_is_per_model_so_no_key_retries_the_busy_model(tmp_path):
    # Every key reaches the same overloaded backend, so a per-KEY cooldown would
    # spend the whole pool collecting the identical 503.
    members = [_sync_member(f"fp{i}") for i in range(4)]
    pool = _pool(members, tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    pool.mark_unavailable(M1)
    for _ in members:
        assert pool.lease_sync(RANKED)[1] == M2


def test_the_whole_chain_cooling_waits_rather_than_failing(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    for m in RANKED:
        pool.mark_unavailable(m)
    kind, _idx, _model, wait = pool._select(RANKED, pool._limits_by_model(RANKED))
    assert kind == "wait" and 0 < wait <= keypool.OVERLOAD_COOLDOWN_S


def test_the_cooldown_expires_and_the_model_returns(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    pool.mark_unavailable(M1, seconds=0.01)
    time.sleep(0.02)
    assert pool.lease_sync(RANKED)[1] == M1, "a spike is temporary, not a write-off"


def test_a_cooling_model_with_no_daily_headroom_does_not_cause_a_wait(tmp_path):
    # Cooling only matters where there is still quota to come back to. A model
    # that is BOTH cooling and RPD-spent must not hold up the vertex fallback.
    pool = _pool([_sync_member("fp1"), _sync_member(None, kind="vertex")], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    for m in RANKED:
        pool.mark_unavailable(m)
        pool._state.incr("fp1", m, 20)
    kind, _idx, _model, _wait = pool._select(RANKED, pool._limits_by_model(RANKED))
    assert kind == "vertex"


def test_vertex_avoids_a_model_that_is_cooling_down(tmp_path):
    # Vertex normally keeps the preferred model (it is not metered by this
    # pool). A 503 is the exception: the shortage is the model's, not the lane's.
    members = [_sync_member("fp1"), _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    pool.mark_unavailable(M1)
    for m in RANKED:
        pool._state.incr("fp1", m, 20)
    member, model = pool.lease_sync(RANKED)
    assert (member["kind"], model) == ("vertex", M2)


def test_vertex_keeps_the_preferred_model_when_the_whole_chain_is_cooling(tmp_path):
    members = [_sync_member("fp1"), _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    for m in RANKED:
        pool.mark_unavailable(m)
        pool._state.incr("fp1", m, 20)
    assert pool.lease_sync(RANKED)[1] == M1


def test_generate_spills_to_the_next_model_on_503_without_burning_the_day(tmp_path):
    seen = []

    def responder(model, contents, config):
        seen.append(model)
        if model == M1:
            raise _Overload()
        return _resp("ok")

    pool = _pool([{"client": _client(responder), "kind": "free", "fp": "fp1"}],
                 tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    out = asyncio.run(pool.generate(model=RANKED, contents="x", config=None))
    assert out.text == "ok"
    assert seen == [M1, M2], "must move on, not retry the model Google just refused"
    # THE point: M1 keeps its allowance. set_exhausted would have written 20.
    assert pool._state.get("fp1", M1) == 1


def test_generate_gives_up_on_a_bounded_number_of_overloads(tmp_path, monkeypatch):
    # Google being broadly down must surface as an error, not an endless loop.
    monkeypatch.setattr(keypool, "OVERLOAD_COOLDOWN_S", 0.01)
    calls = []

    def responder(model, contents, config):
        calls.append(model)
        raise _Overload()

    pool = _pool([{"client": _client(responder), "kind": "free", "fp": "fp1"}],
                 tmp_path, limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    with pytest.raises(_Overload):
        asyncio.run(pool.generate(model=RANKED, contents="x", config=None))
    assert len(calls) <= keypool.OVERLOAD_MAX_RETRIES + 1


# -- what a 429 is allowed to cost -------------------------------------------
# set_exhausted stamps the DAILY ceiling. Applied to a per-minute rejection it
# throws away an entire allowance, which is what emptied the author's free tier
# in a single run: three keys reading 500/500 on a lite after a few dozen calls.
def test_quota_scope_reads_the_payload_not_the_status_line():
    day = RuntimeError("429 quota metric GenerateRequestsPerDayPerProjectPerModel")
    minute = RuntimeError("429 RESOURCE_EXHAUSTED 'retryDelay': '22s'")
    bare = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded "
        "your current quota, please check your plan and billing details.'}}")
    assert keypool._quota_scope(day) == "day"
    assert keypool._quota_scope(minute) == "minute"
    assert keypool._quota_scope(bare) == "unknown"


def test_an_unproven_429_parks_the_pair_instead_of_spending_its_day(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path)
    member = pool._members[0]
    assert pool.mark_exhausted(member, FLASH, RuntimeError("429")) == "cooldown"
    assert pool._state.get("fp1", FLASH) == 0, "no allowance may be written off"


def test_a_second_unproven_429_on_the_same_pair_retires_the_day(tmp_path):
    # A pair that is genuinely spent costs two wasted calls to discover, and
    # then stops being tried. That is the price of not trusting the first one.
    pool = _pool([_sync_member("fp1")], tmp_path)
    member = pool._members[0]
    pool.mark_exhausted(member, FLASH, RuntimeError("429"))
    assert pool.mark_exhausted(member, FLASH, RuntimeError("429")) == "day"
    assert pool._state.get("fp1", FLASH) >= keypool.LIMITS[FLASH]["rpd"]


def test_a_429_naming_a_per_day_metric_is_believed_at_once(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path)
    exc = RuntimeError("429 quota_metric: ...GenerateRequestsPerDayPerProjectPerModel")
    assert pool.mark_exhausted(pool._members[0], FLASH, exc) == "day"
    assert pool._state.get("fp1", FLASH) >= keypool.LIMITS[FLASH]["rpd"]


def test_strikes_are_counted_per_pair_not_per_key(tmp_path):
    pool = _pool([_sync_member("fp1")], tmp_path)
    member = pool._members[0]
    pool.mark_exhausted(member, M1, RuntimeError("429"))
    assert pool.mark_exhausted(member, M2, RuntimeError("429")) == "cooldown", \
        "a strike on one model must not retire a different one"


def test_mark_exhausted_without_the_exception_still_retires_the_day(tmp_path):
    # The old call shape: no payload to judge, so the caller is taken at its word.
    pool = _pool([_sync_member("fp1")], tmp_path)
    assert pool.mark_exhausted(pool._members[0], FLASH) == "day"
    assert pool._state.get("fp1", FLASH) >= keypool.LIMITS[FLASH]["rpd"]


def test_a_parked_pair_is_skipped_rather_than_waited_for(tmp_path):
    pool = _pool([_sync_member("fp1"), _sync_member("fp2")], tmp_path)
    pool.mark_exhausted(pool._members[0], FLASH, RuntimeError("429"))
    member, _m = pool.lease_sync(FLASH, max_wait=0.0)
    assert member["fp"] == "fp2"


def test_vertex_outranks_waiting_out_a_park_but_not_an_rpm_window(tmp_path):
    # THE ordering the author asked for: walk the free tier fast, never stall on
    # a guess, then pay. An RPM window is our own counter and certain to clear,
    # so it still outranks Vertex.
    members = [_sync_member("fp1"), _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path, limits={FLASH: {"rpm": 1, "rpd": 50}})
    pool.mark_exhausted(members[0], FLASH, RuntimeError("429"))
    assert pool.lease_sync(FLASH, max_wait=0.0)[0]["kind"] == "vertex"

    fresh = _pool([_sync_member("fp3"), _sync_member(None, kind="vertex")],
                  tmp_path, limits={FLASH: {"rpm": 1, "rpd": 50}})
    fresh.lease_sync(FLASH)                       # burns the one RPM slot
    kind, _idx, _model, wait = fresh._select([FLASH], fresh._limits_by_model([FLASH]))
    assert kind == "wait" and wait > 0, "an RPM window is still worth waiting out"


def test_free_pair_count_sizes_a_walk_through_the_free_tier(tmp_path):
    members = [_sync_member("fp1"), _sync_member("fp2"), _sync_member("fp3"),
               _sync_member(None, kind="vertex")]
    pool = _pool(members, tmp_path)
    assert pool.free_pair_count(RANKED) == 9      # 3 keys x 3 models, vertex excluded
    assert pool.free_pair_count(FLASH) == 3


def test_generate_reaches_vertex_after_every_free_pair_is_spent(tmp_path):
    # The failure this exists to stop: giving up one lease before the backstop.
    seen = []

    def responder(model, contents, config):
        seen.append(model)
        raise RuntimeError("429 quota metric RequestsPerDayPerProjectPerModel")

    free = [{"client": _client(responder), "kind": "free", "fp": f"fp{i}"}
            for i in range(3)]
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex",
              "fp": None}
    pool = _pool(free + [vertex], tmp_path,
                 limits={m: {"rpm": 5, "rpd": 20} for m in RANKED})
    resp = asyncio.run(pool.generate(model=RANKED, contents="x", config=None))
    assert resp.text == "VERTEX"
    assert len(seen) == 9, "every (key, model) pair must be tried before paying"
