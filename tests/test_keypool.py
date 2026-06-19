import asyncio
import json
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
    assert created[0] == {"api_key": "k1"}
    assert created[-1] == {"vertexai": True, "project": "proj", "location": "global"}


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
