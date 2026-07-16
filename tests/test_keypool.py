import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def _pool(members, tmp_path):
    st = keypool.UsageState(tmp_path / "s.json")
    st.load()
    return keypool.KeyPool(members, st)


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


def test_generate_marks_key_exhausted_on_429_then_fails_over(tmp_path):
    def boom(*_):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    free = {"client": _client(boom), "kind": "free", "fp": "fp1"}
    vertex = {"client": _client(lambda *_: _resp("VERTEX")), "kind": "vertex", "fp": None}
    pool = _pool([free, vertex], tmp_path)
    resp = asyncio.run(pool.generate(model=FLASH, contents="x", config=None))
    assert resp.text == "VERTEX"
    assert pool._state.get("fp1", FLASH) >= keypool.LIMITS[FLASH]["rpd"]


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
    kind, idx, wait = pool._select(FLASH, keypool.LIMITS[FLASH])
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
    kind, idx, wait = pool._select(UNKNOWN_MODEL, limits)
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
    assert pool._select(FLASH, keypool.LIMITS[FLASH])[0] == "none"

    # Cross midnight into D+1.
    monkeypatch.setattr(keypool, "pacific_today", lambda: "2020-01-02")
    kind, idx, _ = pool._select(FLASH, keypool.LIMITS[FLASH])
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
