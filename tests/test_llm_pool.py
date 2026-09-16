"""The resume tailor's pooled Gemini lane (gemini_auth = "pool").

Before this, the tailor held ONE client: 'vertex' billed every call to the
cloud project, and 'api_key' used a single RESUME_TAILOR_GEMINI_API_KEY with no
rotation, no per-day accounting and no backstop -- it just failed once that key
hit its cap. "pool" borrows the job scorer's keypool.KeyPool: every key in
GEMINI_API_KEYS, rate-gated per model, with Vertex as the spillover.

The tailor keeps its OWN retry envelope (escalating timeouts, 429 backoff), so
it leases a member and drives the request itself rather than handing the call to
pool.generate(). No real API calls, no network, no billing.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))
sys.path.insert(0, str(REPO / "pipeline"))

import keypool  # noqa: E402
from resume_tailor import config, llm  # noqa: E402

MODEL = "gemini-3.8-flash"


def _ok_resp(text='{"ok": 1}'):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=2),
    )


class _Quota(Exception):
    def __str__(self):
        return "429 RESOURCE_EXHAUSTED"


def _member(fp, responder, kind="free"):
    def gen(*, model, contents, config):
        return responder(model)
    return {"client": SimpleNamespace(models=SimpleNamespace(generate_content=gen)),
            "kind": kind, "fp": fp}


class _Overload(Exception):
    """The shape google-genai raises for a 503 (APIError sets .code/.status)."""

    code = 503
    status = "UNAVAILABLE"

    def __str__(self):
        return ("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model "
                "is currently experiencing high demand. Spikes in demand are "
                "usually temporary. Please try again later.', "
                "'status': 'UNAVAILABLE'}}")


class FakePool:
    """Hands out `members` in order; records what got marked exhausted."""

    def __init__(self, members):
        self.members = list(members)
        self.leased: list = []
        self.exhausted: list = []
        self.unavailable: list = []
        self.cooling: set = set()

    def lease_sync(self, model, **kw):
        # Mirrors KeyPool.lease_sync: takes a ranked chain, returns the pair it
        # actually chose. This fake picks the head of the chain, skipping any
        # model parked by mark_unavailable -- the RPM/RPD side of the spill is
        # KeyPool's own business and is tested there.
        chain = keypool.ranked_models(model)
        live = [m for m in chain if m not in self.cooling] or chain
        if not self.members:
            raise keypool.PoolError(f"No usable pool member for {'/'.join(chain)}")
        m = self.members.pop(0)
        self.leased.append((m["fp"], live[0]))
        return m, live[0]

    def mark_exhausted(self, member, model, exc=None):
        self.exhausted.append((member["fp"], model))
        return "day"

    def free_pair_count(self, models):
        free = [m for m in self.members if m["kind"] == "free"]
        return max(1, len(free)) * max(1, len(keypool.ranked_models(models)))

    def mark_unavailable(self, model, seconds=None):
        self.unavailable.append(model)
        self.cooling.add(model)


@pytest.fixture
def pooled(monkeypatch):
    """gemini_auth='pool', creds present, sleeps recorded rather than slept."""
    recorded: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: recorded.append(s))
    monkeypatch.setattr(llm.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(llm.config, "_config_json", lambda: {"gemini_auth": "pool"})
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "AQ.key1,AQ.key2")
    llm.reset_usage()
    llm.reset_pool()
    yield recorded
    llm.reset_usage()
    llm.reset_pool()


# -- auth mode ---------------------------------------------------------------
def test_gemini_auth_accepts_pool(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_GEMINI_AUTH", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"gemini_auth": "pool"})
    assert config.gemini_auth() == "pool"


def test_check_creds_accepts_scorer_keys_for_pool_mode(monkeypatch, pooled):
    monkeypatch.setenv("GEMINI_API_KEYS", "AQ.key1")
    llm._check_creds()          # must not raise


def test_check_creds_fails_fast_when_pool_mode_has_no_credentials(monkeypatch, pooled):
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm.config, "GCP_PROJECT", "")
    with pytest.raises(llm.LLMError) as e:
        llm._check_creds()
    assert e.value.kind == "config"


# -- leasing -----------------------------------------------------------------
def test_pool_mode_calls_the_leased_members_client(monkeypatch, pooled):
    pool = FakePool([_member("fp1", lambda m: _ok_resp('{"v": 1}'))])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    out = llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert out == {"v": 1}
    assert pool.leased == [("fp1", MODEL)]


def test_a_rate_limited_free_key_is_retired_and_the_next_key_runs_the_call(
        monkeypatch, pooled):
    def boom(model):
        raise _Quota()
    pool = FakePool([_member("fp1", boom),
                     _member("fp2", lambda m: _ok_resp('{"v": 2}'))])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    out = llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert out == {"v": 2}
    assert pool.exhausted == [("fp1", MODEL)], "the dead key must be marked"
    assert pool.leased == [("fp1", MODEL), ("fp2", MODEL)]
    # Rotating to a live key is instant -- the 30s+ backoff is for the case
    # where there is nothing left to rotate TO.
    assert not [s for s in pooled if s >= llm.RATE_LIMIT_BASE_SLEEP]


def test_a_rate_limited_vertex_member_backs_off_instead_of_rotating(
        monkeypatch, pooled):
    def boom(model):
        raise _Quota()
    pool = FakePool([_member(None, boom, kind="vertex"),
                     _member(None, lambda m: _ok_resp('{"v": 3}'), kind="vertex")])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    out = llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert out == {"v": 3}
    assert pool.exhausted == [], "a vertex member has no per-day quota to retire"
    assert [s for s in pooled if s >= llm.RATE_LIMIT_BASE_SLEEP], \
        "vertex 429 must use the backoff budget"


def test_rotation_is_bounded_when_every_key_is_rate_limited(monkeypatch, pooled):
    def boom(model):
        raise _Quota()
    pool = FakePool([_member("fp%d" % i, boom) for i in range(3)])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    with pytest.raises(llm.LLMError):
        llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert len(pool.exhausted) == 3, "every dead key gets retired"


def test_pool_exhaustion_surfaces_as_an_llm_error(monkeypatch, pooled):
    pool = FakePool([])         # lease_sync raises PoolError immediately
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    with pytest.raises(llm.LLMError):
        llm._call_gemini("sys", "user", MODEL, json_out=True)


# -- pool construction -------------------------------------------------------
def _capture_from_env(monkeypatch, built):
    def fake_from_env(*, state_path=None, limits=None, http_timeout_s=None):
        built.append({"state_path": Path(state_path), "limits": limits,
                      "http_timeout_s": http_timeout_s})
        return FakePool([])

    monkeypatch.setattr(keypool.KeyPool, "from_env", staticmethod(fake_from_env))


def test_the_pool_is_built_once_and_reset_rebuilds_it(monkeypatch, pooled):
    built: list[dict] = []
    _capture_from_env(monkeypatch, built)
    llm._pool()
    llm._pool()
    assert len(built) == 1, "clients are expensive -- build the pool once"
    llm.reset_pool()
    llm._pool()
    assert len(built) == 2


def test_the_pool_shares_the_scorers_state_file_and_limits(monkeypatch, pooled):
    built: list[dict] = []
    _capture_from_env(monkeypatch, built)
    monkeypatch.setattr(llm, "_pool_limits", lambda: {MODEL: {"rpm": 5, "rpd": 20}})
    llm._pool()
    got = built[0]
    # Same file the scorer writes: the two lanes spend ONE per-key daily quota,
    # so separate counters would double-spend it into real 429s.
    assert got["state_path"] == Path(config.SCRAPE_DIR) / "score_state.json"
    assert got["limits"] == {MODEL: {"rpm": 5, "rpd": 20}}
    # Pool clients are built once, so they cannot escalate per attempt the way
    # _build_client does; the longest slot in the schedule is the honest choice.
    assert got["http_timeout_s"] == config.tailor_timeout_schedule()[-1]


# -- ranked fallback models --------------------------------------------------
# Free-tier quota is metered per (key, model), so a fallback model is a separate
# daily allowance. The tailor asks the pool for a chain; the pool decides.

def _simple(monkeypatch, model="gemini-3.8-flash"):
    """'simple' mode: every tier resolves to one model, so one chain fits all."""
    monkeypatch.setenv("RESUME_TAILOR_MODEL_MODE", "simple")
    monkeypatch.setenv("RESUME_TAILOR_MODEL_ALL", model)


def _tiers(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_MODEL_MODE", "tiers")


def test_simple_mode_uses_the_one_shared_chain(monkeypatch):
    _simple(monkeypatch)
    monkeypatch.setattr(llm.config, "_config_json",
                        lambda: {"tailor_fallback_models": ["gemini-3.7-flash"]})
    monkeypatch.delenv("RESUME_TAILOR_FALLBACK_MODELS", raising=False)
    assert llm.config.gemini_fallback_models() == ["gemini-3.7-flash"]

    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_MODELS",
                       "gemini-3.6-flash, gemini-3.5-flash;gemini-3.6-flash")
    assert llm.config.gemini_fallback_models() == [
        "gemini-3.6-flash", "gemini-3.5-flash"]        # order kept, dupes dropped
    # Every tier resolves to the same model, so every tier gets the same chain.
    for tier in (config.TIER_FLASH_LITE, config.TIER_FLASH, config.TIER_PRO):
        assert llm.config.gemini_fallback_models(tier) == [
            "gemini-3.6-flash", "gemini-3.5-flash"]


def test_tiers_mode_gives_each_tier_its_own_chain(monkeypatch):
    # The tiers are quality classes whose free quota runs the OTHER way -- a lite
    # allows 500 requests/day per key, a full Flash 20. A shared chain would send
    # the cheap high-volume selection pass into the allowance the cover letter
    # and the scorer's deep stage depend on.
    _tiers(monkeypatch)
    monkeypatch.setattr(llm.config, "_config_json", lambda: {})
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH_LITE", "gemini-3.5-flash-lite")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH", "gemini-3.7-flash")
    monkeypatch.delenv("RESUME_TAILOR_FALLBACK_PRO", raising=False)

    assert llm.config.gemini_fallback_models(config.TIER_FLASH_LITE) ==         ["gemini-3.5-flash-lite"]
    assert llm.config.gemini_fallback_models(config.TIER_FLASH) == ["gemini-3.7-flash"]
    assert llm.config.gemini_fallback_models(config.TIER_PRO) == []


def test_tiers_mode_never_borrows_the_shared_chain(monkeypatch):
    # The regression this split exists for: in 'tiers' mode the one-model list is
    # not a default to fall back on -- it belongs to a mode where every tier is
    # the same model. Borrowing it is what sent flash_lite onto 20/day models.
    _tiers(monkeypatch)
    monkeypatch.setattr(llm.config, "_config_json", lambda: {})
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_MODELS", "gemini-3.7-flash")
    monkeypatch.delenv("RESUME_TAILOR_FALLBACK_FLASH_LITE", raising=False)
    assert llm.config.gemini_fallback_models(config.TIER_FLASH_LITE) == []
    assert llm.config.gemini_fallback_models(None) == []


def test_a_tier_falls_back_only_inside_its_own_quota_class(monkeypatch):
    """The property that matters, stated in free-tier numbers rather than names."""
    _tiers(monkeypatch)
    monkeypatch.setattr(llm.config, "_config_json", lambda: {})
    monkeypatch.setenv("RESUME_TAILOR_MODEL_FLASH_LITE", "gemini-3.1-flash-lite")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH_LITE", "gemini-3.5-flash-lite")
    monkeypatch.setenv("RESUME_TAILOR_MODEL_FLASH", "gemini-3.8-flash")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH", "gemini-3.7-flash")
    for tier in (config.TIER_FLASH_LITE, config.TIER_FLASH):
        chain = keypool.ranked_models(config.model_for(tier),
                                      llm.config.gemini_fallback_models(tier))
        rpd = {keypool.LIMITS[m]["rpd"] for m in chain}
        assert len(rpd) == 1, f"{tier} mixes quota classes: {chain}"


def test_the_pool_is_asked_for_the_tiers_own_chain(pooled, monkeypatch):
    _tiers(monkeypatch)
    monkeypatch.setattr(llm.config, "_config_json", lambda: {"gemini_auth": "pool"})
    monkeypatch.setenv("RESUME_TAILOR_MODEL_FLASH", "gemini-3.8-flash")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH",
                       "gemini-3.7-flash,gemini-3.6-flash")
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_FLASH_LITE", "gemini-3.5-flash-lite")
    seen: list = []

    class ChainPool(FakePool):
        def lease_sync(self, model, **kw):
            seen.append(list(keypool.ranked_models(model)))
            return super().lease_sync(model, **kw)

    monkeypatch.setattr(llm, "_pool",
                        lambda: ChainPool([_member("fp1", lambda m: _ok_resp("OK"))]))
    assert llm.call("sys", "user", config.TIER_FLASH) == "OK"
    assert seen == [["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]],         "the step's own model must lead, and the LITE chain must not appear"


def test_the_step_model_is_never_duplicated_in_the_chain(pooled, monkeypatch):
    # A fallback list that repeats the step's model would make the pool try the
    # same pair twice before moving on -- pure latency, no extra quota.
    _simple(monkeypatch)
    monkeypatch.setenv("RESUME_TAILOR_FALLBACK_MODELS",
                       "gemini-3.8-flash,gemini-3.7-flash")
    seen: list = []

    class ChainPool(FakePool):
        def lease_sync(self, model, **kw):
            seen.append(list(keypool.ranked_models(model)))
            return super().lease_sync(model, **kw)

    monkeypatch.setattr(llm, "_pool",
                        lambda: ChainPool([_member("fp1", lambda m: _ok_resp("OK"))]))
    llm._call_gemini("sys", "user", "gemini-3.8-flash")
    assert seen == [["gemini-3.8-flash", "gemini-3.7-flash"]]


def test_the_call_and_the_retirement_use_the_model_the_pool_chose(pooled, monkeypatch):
    # The pool may hand back a fallback. The request must go to THAT model, and
    # a 429 must retire THAT pair -- retiring the requested model instead would
    # burn an allowance the key never spent.
    called: list = []

    def boom(model):
        called.append(model)
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    class SpillPool(FakePool):
        def lease_sync(self, model, **kw):
            m = self.members.pop(0)
            chosen = "gemini-3.6-flash"          # not what the step asked for
            self.leased.append((m["fp"], chosen))
            return m, chosen

    pool = SpillPool([_member("fp1", boom),
                      _member("fp2", lambda m: (called.append(m), _ok_resp("OK"))[1])])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    assert llm._call_gemini("sys", "user", "gemini-3.8-flash") == "OK"
    assert called == ["gemini-3.6-flash", "gemini-3.6-flash"]
    assert pool.exhausted == [("fp1", "gemini-3.6-flash")]


def test_usage_is_attributed_to_the_model_that_actually_ran(pooled, monkeypatch):
    class SpillPool(FakePool):
        def lease_sync(self, model, **kw):
            m = self.members.pop(0)
            return m, "gemini-3.6-flash"

    monkeypatch.setattr(llm, "_pool",
                        lambda: SpillPool([_member("fp1", lambda m: _ok_resp("OK"))]))
    llm._call_gemini("sys", "user", "gemini-3.8-flash")
    assert [u["model"] for u in llm.USAGE] == ["gemini-3.6-flash"], \
        "cost accounting must name the allowance the run really spent"


def test_usage_never_inherits_the_previous_calls_model(pooled, monkeypatch):
    # The thread-local is per-attempt state. A call whose _invoke does not set it
    # must fall back to the model it asked for, not to whatever ran last on this
    # thread -- the tailor runs its jobs on a long-lived ThreadPoolExecutor, so a
    # stale value would survive for the life of the worker.
    monkeypatch.setattr(llm, "_pool",
                        lambda: FakePool([_member("fp1", lambda m: _ok_resp("OK"))]))
    llm._call_gemini("sys", "user", "gemini-3.8-flash")

    monkeypatch.setattr(llm, "_invoke", lambda *a, **k: _ok_resp("OK2"))
    llm._call_gemini("sys", "user", "gemini-3.5-flash")
    assert [u["model"] for u in llm.USAGE] == ["gemini-3.8-flash", "gemini-3.5-flash"]


# -- 503 / model overload ----------------------------------------------------
# The failure this exists to stop: a tailor run died on
#   "Gemini call failed after retries (gemini-3.8-flash): 503 UNAVAILABLE."
# The fallback chain was configured and never used, because a 503 is not a
# quota error -- so nothing was retired, every retry re-selected the same
# overloaded model, and three attempts burned ~4.5s before giving up.
FALLBACK = "gemini-3.7-flash"


def test_a_503_moves_to_the_next_model_in_the_chain(monkeypatch, pooled):
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [FALLBACK])

    def busy(model):
        raise _Overload()

    pool = FakePool([_member("fp1", busy),
                     _member("fp2", lambda m: _ok_resp('{"v": 7}'))])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    out = llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert out == {"v": 7}
    assert pool.unavailable == [MODEL], "the busy model must be parked"
    assert pool.leased == [("fp1", MODEL), ("fp2", FALLBACK)]


def test_an_overloaded_model_is_never_retired_for_the_day(monkeypatch, pooled):
    # set_exhausted writes the DAILY ceiling. Spending a whole allowance on a
    # demand spike that clears in a minute is the expensive mistake here.
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [FALLBACK])

    def busy(model):
        raise _Overload()

    pool = FakePool([_member("fp1", busy), _member("fp2", lambda m: _ok_resp())])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    llm._call_gemini("sys", "user", MODEL)
    assert pool.exhausted == []


def test_rotating_past_a_503_does_not_sit_out_a_rate_limit_backoff(monkeypatch, pooled):
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [FALLBACK])

    def busy(model):
        raise _Overload()

    pool = FakePool([_member("fp1", busy), _member("fp2", lambda m: _ok_resp())])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    llm._call_gemini("sys", "user", MODEL)
    assert not [s for s in pooled if s >= llm.RATE_LIMIT_BASE_SLEEP], (
        "a 503 is answered by changing model, not by sleeping out a 429 budget")


def test_a_persistent_503_is_bounded_and_says_what_happened(monkeypatch, pooled):
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [FALLBACK])

    def busy(model):
        raise _Overload()

    pool = FakePool([_member(f"fp{i}", busy) for i in range(20)])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    with pytest.raises(llm.LLMError) as e:
        llm._call_gemini("sys", "user", MODEL)
    assert "overload" in str(e.value).lower()
    assert len(pool.leased) <= llm.OVERLOAD_MAX_RETRIES + 1


def test_a_503_gets_a_real_backoff_when_there_is_nothing_to_rotate_to(
        monkeypatch, pooled):
    # Vertex-only pool, no fallback chain: the model cannot change, so holding
    # off is the only remedy. The old code slept 1.5s and 3.0s and gave up.
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [])

    def busy(model):
        raise _Overload()

    pool = FakePool([_member(None, busy, kind="vertex") for _ in range(20)])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    with pytest.raises(llm.LLMError):
        llm._call_gemini("sys", "user", MODEL)
    assert max(pooled) >= llm.OVERLOAD_BASE_SLEEP


def test_the_rotation_ceiling_is_sized_from_the_pool_not_a_constant(monkeypatch,
                                                                    pooled):
    # A flat ceiling below (free keys x chain length) turns "fall back to
    # Vertex" into "give up": the author's run died reporting 12 rotations with
    # 3 keys x 5 models still to walk and the backstop one lease away.
    chain = [FALLBACK, "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: chain)
    pool = FakePool([_member(f"fp{i}", lambda m: _ok_resp()) for i in range(3)])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    assert llm._rotation_budget(MODEL, None) == 3 * 5 + 1


def test_a_pool_walk_longer_than_the_flat_ceiling_still_reaches_the_backstop(
        monkeypatch, pooled):
    chain = [FALLBACK, "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: chain)

    def boom(model):
        raise _Quota()

    # 15 free pairs' worth of 429s -- more than POOL_MAX_ROTATIONS -- then Vertex.
    members = [_member(f"fp{i}", boom) for i in range(15)]
    members.append(_member(None, lambda m: _ok_resp('{"v": 9}'), kind="vertex"))
    pool = FakePool(members)
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    out = llm._call_gemini("sys", "user", MODEL, json_out=True)
    assert out == {"v": 9}
    assert len(pool.exhausted) == 15 > llm.POOL_MAX_ROTATIONS


def test_the_429_payload_is_handed_to_the_pool_to_judge(monkeypatch, pooled):
    seen = []

    class Pool(FakePool):
        def mark_exhausted(self, member, model, exc=None):
            seen.append(exc)
            return super().mark_exhausted(member, model)

    def boom(model):
        raise _Quota()

    monkeypatch.setattr(llm.config, "gemini_fallback_models", lambda tier=None: [])
    pool = Pool([_member("fp1", boom), _member("fp2", lambda m: _ok_resp())])
    monkeypatch.setattr(llm, "_pool", lambda: pool)
    llm._call_gemini("sys", "user", MODEL)
    assert seen and isinstance(seen[0], _Quota), \
        "only the payload can say whether the day is really gone"
