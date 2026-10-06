"""Jev gates the AI-writing sweep's calls (`sweep.sweep_items(judge=)`).

With Jev on, `jev_assist.sweep_flags` reads every bullet the sweep may rewrite, one
request per item, for the tells of the user's banned patterns. An item makes its
model call only when one of its bullets carries a tell at `jev_assist.SWEEP_FLAG` or
more, or when a detector finding forces it: an `itemcheck` finding of a repaired
tier or an `aiwriting` phrasing hit. The tells ride in the payload as each bullet's
`judge_flags`, with a rule in the system prompt and a line in the closing that name
them. With Jev off, or when its request fails, every item is swept with its
Jev-free prompts.

Nothing here reaches a model or Jev: `sweep.call` is the recorder from
`test_item_sweep.py`, and every judge is a fake.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from resume_tailor import aiwriting, compose, config, itemcheck, jev_assist, sweep  # noqa: E402

import test_item_sweep as item_sweep  # noqa: E402 - sibling test module, no pkg import

# The sweep module's fixture, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
engine = item_sweep.engine

ITEM = item_sweep.ITEM
CLEAN = item_sweep.CLEAN
Recorder = item_sweep.Recorder
_answer = item_sweep._answer
_install = item_sweep._install
_sel = item_sweep._sel

# A P2-only item, from test_item_sweep's policy test: a rule-of-three hit and nothing
# the sweep repairs by default.
_THREE = {
    "a1": ("Built a batched async fetcher, a retry queue, and a nightly backfill "
           "for the ingestion job across 12 source systems."),
    "a2": ("Raised coverage on the billing service to 81% with unit, contract, "
           "and integration suites for its three least-covered modules."),
    "a3": CLEAN["a3"],
}
_V1 = "Designed a hiking route planner that ranks 40 trails by weather."


@pytest.fixture(autouse=True)
def _fresh_usage():
    jev_assist.reset_usage()
    yield
    jev_assist.reset_usage()


class Reads:
    """Says yes to the tells named for a bullet's text and no to every other:
    `tells` maps a bullet text to the question ids it flags ("hype", "three", ...)."""

    def __init__(self, tells=None):
        self.tells = tells or {}
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        out = {}
        for qid in questions:
            tell, i = qid.rsplit("_", 1)
            said = tell in self.tells.get(state["bullets"][int(i)], ())
            out[qid] = jev.Answer(kind="noul", noul=0.9 if said else 0.1)
        return out


class Failing:
    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        raise RuntimeError("the request was rejected")


def _two_items():
    sel = _sel()
    sel["projects"].append({"name": "Trailhead", "groups": [["v1"]]})
    return sel, dict(CLEAN, v1=_V1)


def _sweep(monkeypatch, bullets, sel=None, first=None, **kw):
    rec = _install(monkeypatch, Recorder(first=first))
    result = sweep.sweep_items("", "Data Engineer", sel or _sel(), bullets, **kw)
    return rec, result


# ── the fixtures are honest ──────────────────────────────────────────────────
def test_the_fixture_items_carry_no_repaired_finding(engine):
    """The gate's skip rests on this: no fixture item has a finding the sweep repairs
    by default, and no bullet has a phrasing hit."""
    for text in list(CLEAN.values()) + list(_THREE.values()) + [_V1]:
        assert aiwriting.resume_violations(text) == []
    assert itemcheck.item_findings(ITEM, list(CLEAN.items())) == []
    assert itemcheck.item_findings("Trailhead", [("v1", _V1)]) == []
    assert [f.tier for f in itemcheck.item_findings(ITEM, list(_THREE.items()))] == [
        itemcheck.P2]


# ── the gate ────────────────────────────────────────────────────────────────
def test_an_item_jev_reads_as_clean_makes_no_call(engine, monkeypatch):
    """No tell and no detector finding: the sweep's SCOPE rule would hand every bullet
    back as it arrived, so the call is skipped. The recorder raises on any call."""
    bullets = dict(CLEAN)
    judge = Reads()
    rec, result = _sweep(monkeypatch, bullets, judge=judge)
    assert rec.calls == []
    assert bullets == CLEAN
    assert result.skipped == (ITEM,)
    assert (result.calls, result.items) == (0, 0)
    assert result.judge_flags == ()
    assert len(judge.requests) == 1


def test_the_fake_reads_the_clean_item_as_clean(engine, monkeypatch):
    rec, result = _sweep(monkeypatch, dict(CLEAN), judge=jev.FakeJev())
    assert rec.calls == []
    assert result.skipped == (ITEM,)


def test_one_read_per_item_over_the_text_the_sweep_would_send(engine, monkeypatch):
    sel, bullets = _two_items()
    judge = Reads()
    _rec, result = _sweep(monkeypatch, bullets, sel=sel, judge=judge)
    assert [state for state, _q in judge.requests] == [
        {"entry": ITEM, "bullets": [CLEAN["a1"], CLEAN["a2"], CLEAN["a3"]]},
        {"entry": "Trailhead", "bullets": [_V1]}]
    assert result.skipped == (ITEM, "Trailhead")
    assert jev_assist.usage(jev_assist.STEP_SWEEP_GATE)["requests"] == 2


def test_a_flagged_item_is_sent_with_its_flags_and_the_rule(engine, monkeypatch):
    judge = Reads({CLEAN["a2"]: ("hype", "three")})
    rec, result = _sweep(monkeypatch, dict(CLEAN), first=_answer(), judge=judge)
    assert rec.kinds == ["sweep"]
    _kind, system, user = rec.calls[0]
    assert system == sweep._SWEEP_SYSTEM + sweep._SWEEP_FLAG_RULE
    assert sweep._SWEEP_FLAG_CLOSING in user
    sent = {b["gkey"]: b for b in rec.payload(0)["bullets"]}
    assert sent["a2"]["judge_flags"] == ["hype words", "padded list of three"]
    assert sent["a1"]["judge_flags"] == [] and sent["a3"]["judge_flags"] == []
    assert result.judge_flags == (
        sweep.JudgeFlag("a2", ITEM, ("hype words", "padded list of three")),)
    assert result.skipped == ()
    assert (result.calls, result.items) == (1, 1)


def test_only_the_flagged_item_is_sent(engine, monkeypatch):
    sel, bullets = _two_items()
    judge = Reads({_V1: ("filler",)})
    rec, result = _sweep(monkeypatch, bullets, sel=sel, first=_answer(), judge=judge)
    assert rec.kinds == ["sweep"]
    assert rec.payload(0)["item"] == "Trailhead"
    assert result.skipped == (ITEM,)
    assert result.judge_flags == (sweep.JudgeFlag("v1", "Trailhead",
                                                  ("filler or vague impact",)),)


def test_a_flagged_rewrite_is_held_to_the_same_acceptance_check(engine, monkeypatch):
    """A flag buys a call and nothing else: the rewrite still has to keep the opening
    verb, the facts and the budget, or the original ships."""
    dropped = ("Raised test coverage on the billing service with pytest suites for its "
               "three least-covered modules.")
    judge = Reads({CLEAN["a2"]: ("filler",)})
    bullets = dict(CLEAN)
    _rec, result = _sweep(monkeypatch, bullets, first=_answer(a2=dropped), judge=judge)
    assert bullets == CLEAN
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a2", sweep.REASON_FACTS)]


@pytest.mark.parametrize("bullets,why", [
    pytest.param(dict(CLEAN, a1=item_sweep._BARE), "bare_noun_bullet", id="p1-finding"),
    pytest.param(dict(CLEAN, a1=item_sweep._FORMULAIC), "formulaic opening", id="phrasing"),
])
def test_a_detector_finding_forces_the_call(engine, monkeypatch, bullets, why):
    """Jev reads no tell, and the detectors still force the call. With no tell to
    name, the prompts are the ones the sweep sends with Jev off."""
    off, _result = _sweep(monkeypatch, dict(bullets), first=_answer())
    rec, result = _sweep(monkeypatch, dict(bullets), first=_answer(), judge=Reads())
    assert rec.kinds == ["sweep"]
    sent = {b["gkey"]: b for b in rec.payload(0)["bullets"]}
    assert why in sent["a1"]["findings"] + sent["a1"]["phrasing"]
    assert rec.calls == off.calls
    assert "judge_flags" not in rec.calls[0][2]
    assert result.skipped == ()


def test_a_p2_finding_alone_buys_no_call_and_is_still_reported(engine, monkeypatch):
    """P2 is reported by policy and never reaches the model, so it forces nothing;
    the skipped item still reports it."""
    rec, result = _sweep(monkeypatch, dict(_THREE), judge=Reads())
    assert rec.calls == []
    assert result.skipped == (ITEM,)
    assert [f.detector for f in result.unfixed_p2] == ["rule_of_three"]


def test_with_p2_repaired_a_p2_finding_forces_the_call(engine, monkeypatch):
    monkeypatch.setattr(config, "sweep_p2_enabled", lambda: True)
    rec, result = _sweep(monkeypatch, dict(_THREE), first=_answer(), judge=Reads())
    assert rec.kinds == ["sweep"]
    assert result.skipped == ()


def test_a_bullet_jev_did_not_read_keeps_the_call(engine, monkeypatch):
    """The gate skips only an item whose every bullet Jev read as clean."""
    monkeypatch.setattr(jev_assist, "sweep_flags",
                        lambda entries, judge: {"a1": (), "a2": ()})
    rec, result = _sweep(monkeypatch, dict(CLEAN), first=_answer(), judge=Reads())
    assert rec.kinds == ["sweep"]
    assert result.skipped == ()


# ── without Jev ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("judge", [None, Failing()], ids=["off", "failing"])
def test_without_jev_every_item_is_swept_with_the_prompts_it_had(engine, monkeypatch, judge):
    sel, bullets = _two_items()
    before, _result = _sweep(monkeypatch, dict(bullets), sel=sel, first=_answer())
    rec, result = _sweep(monkeypatch, dict(bullets), sel=sel, first=_answer(), judge=judge)
    assert rec.kinds == ["sweep", "sweep"]
    assert rec.calls == before.calls
    assert all(system == sweep._SWEEP_SYSTEM for _k, system, _u in rec.calls)
    assert result.skipped == () and result.judge_flags == ()
    assert (result.calls, result.items) == (2, 2)


def test_a_failed_read_names_its_fallback(engine, monkeypatch):
    judge = Failing()
    _sweep(monkeypatch, dict(CLEAN), first=_answer(), judge=judge)
    assert judge.calls == 1
    assert jev_assist.usage_line(jev_assist.STEP_SWEEP_GATE).endswith(
        "; fell back to the LLM path (RuntimeError)")


def test_with_jev_off_the_gate_sends_no_request(engine, monkeypatch):
    _sweep(monkeypatch, dict(CLEAN), first=_answer(), judge=None)
    assert jev_assist.usage(jev_assist.STEP_SWEEP_GATE)["requests"] == 0


# ── the added prompt text obeys the rules it states ─────────────────────────
_FLAG_PROMPTS = ("_SWEEP_FLAG_RULE", "_SWEEP_FLAG_CLOSING")


@pytest.mark.parametrize("name", _FLAG_PROMPTS)
def test_the_flag_prompts_are_free_of_the_characters_they_ban(name):
    text = getattr(sweep, name)
    assert chr(0x2014) not in text
    assert " -- " not in text


@pytest.mark.parametrize("name", _FLAG_PROMPTS)
def test_the_flag_prompts_are_free_of_the_phrasing_they_ban(name):
    assert compose.style_violations(getattr(sweep, name)) == []


def test_the_rule_names_every_tell_jev_can_raise():
    for name in jev_assist.SWEEP_QUESTIONS:
        assert name in sweep._SWEEP_FLAG_RULE
    assert "PHRASING" in sweep._SWEEP_FLAG_RULE
    assert sweep._SWEEP_FLAG_CLOSING.startswith(sweep._SWEEP_CLOSING)
