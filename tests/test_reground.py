"""The bounded re-ask that recovers a bullet the FIRST grounding gate deleted.

The gate that runs on rephrase's output is the only fallback-less one: there is no
earlier grounded text to revert to, so a bullet carrying a single unsupported term is
deleted outright. That is the correct call for the fabricated fact it exists to stop, and
a bad outcome for the far commoner case -- a faithful paraphrase that reached for one
summarizing label the atoms never used.

It cost a real resume its lead bullet. A project's overview bullet said "ETL" for a
source -> score -> triage -> tailor flow, the gate deleted it, and the entry shipped as a
single implementation-detail bullet that never said what the project was. The atoms were
correct and the overview atom was authored first, exactly as the ordering fallback
expects; the bullet built from it simply never reached the page.

So this file is about the recovery: it re-asks from the SAME atoms with the offending
terms banned, and re-runs the same pinned gate over the answer, so nothing ungrounded
comes back in through the repair. **Nothing here calls a model** -- `compose.call` is
monkeypatched in every test, and the tests that must not spend a call install a stub that
raises if it is called at all.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import assets, compose, config, measure, verify  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

PROJECT = "INployed"

# Modelled on the entry that failed. `p1` is the overview atom -- the one that says what
# the project IS -- and `p2` is an implementation detail. Neither writes "ETL".
_ATOMS = {
    "p1": {"id": "p1", "_block": PROJECT,
           "what": "a three-subsystem job-discovery pipeline that runs source, score, "
                   "triage, and tailor end to end"},
    "p2": {"id": "p2", "_block": PROJECT,
           "what": "a two-stage relevance scorer served by a quota-aware key pool with "
                   "per-key rate limits"},
}

# What rephrase actually produced: a fair summary of `p1` wearing one label from nowhere.
UNGROUNDED_OVERVIEW = ("Designed a three-subsystem job-discovery pipeline whose ETL flow "
                       "runs source, score, triage, and tailor end to end.")
# The same facts without the stray label. Every distinctive token traces to `p1`.
REGROUNDED_OVERVIEW = ("Designed a three-subsystem job-discovery pipeline that runs "
                       "source, score, triage, and tailor end to end.")
# A detail bullet that was always grounded, so it is never the thing under test.
DETAIL = ("Configured a two-stage relevance scorer served by a quota-aware key pool with "
          "per-key rate limits.")


def _sel():
    return {"experience": [], "leadership": [],
            "projects": [{"name": PROJECT, "groups": [["p1"], ["p2"]]}]}


class Recorder:
    """A `compose.call` replacement. `answer` goes to the re-ask; a second call has
    nowhere to go and raises, so "exactly one re-ask" is enforced by the stub rather than
    only by an assertion afterwards."""

    def __init__(self, answer=None, raises=False):
        self.answer, self.raises = answer, raises
        self.systems, self.users = [], []

    def __call__(self, system, user, tier, **kw):
        self.systems.append(system)
        self.users.append(user)
        if self.raises:
            raise RuntimeError("transport down")
        if self.answer is None:
            raise AssertionError("reground issued a call it should not have")
        answer, self.answer = self.answer, None
        return answer


@pytest.fixture()
def engine(monkeypatch):
    """Every input the gate and the recovery read, pinned. No user data, no config.json."""
    monkeypatch.delenv("RESUME_TAILOR_REGROUND", raising=False)
    monkeypatch.setattr(assets, "atoms_by_id", lambda: {k: dict(v)
                                                        for k, v in _ATOMS.items()})
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)


def _ctx(bullets, report):
    return rt_run.PassCtx(jd="Data engineering role.", job_title="Data Engineer",
                          sel=_sel(), bullets=bullets, verbatim={},
                          reserved=frozenset(), log=lambda _m: None, report=report)


def _answer(**texts):
    return {"bullets": [{"gkey": gk, "text": t} for gk, t in texts.items()]}


def _sent(user: str):
    """The JSON payload out of a recorded user message, PARSED.

    Asserting on substrings of the whole message does not work here: the instruction
    text names "banned_tokens" itself, so a plain `in` check passes even when the key is
    renamed out of the payload and the model is shown nothing. Parsing separates the data
    from the prose about the data."""
    body = user.split("REJECTED BULLETS", 1)[1].split("Return ONLY JSON", 1)[0]
    return json.loads(body[body.index("["):body.rindex("]") + 1])


# ── the fixture has to be honest ─────────────────────────────────────────────

def test_the_overview_bullet_really_is_dropped_by_the_real_gate(engine):
    """Everything below rests on this. If the fixture bullet were grounded, every test
    would pass while proving nothing, because no recovery would ever be attempted."""
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    handled = rt_run._gate(_ctx(bullets, rt_run.RunLog()), stage="rephrase")
    assert handled["p1"] == ["ETL"]
    assert "p1" not in bullets            # deleted, not reverted: no fallback exists
    assert bullets["p2"] == DETAIL        # and the detail bullet is untouched


def test_the_repaired_text_really_is_grounded(engine):
    """The other half: the recovery target must pass the same gate unaided, or a test
    could credit the re-ask for text the gate would delete a second time."""
    src = verify.group_source_text(["p1"], extra=PROJECT)
    assert verify.unseen_tokens(REGROUNDED_OVERVIEW, src) == []


# ── the re-ask reaches the model with what it needs ──────────────────────────

def test_the_banned_tokens_travel_with_the_bullet(engine, monkeypatch):
    """The defect this repairs is a model told to fix something it was never shown. A
    re-ask that does not name the offending term just reproduces it."""
    rec = Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW))
    monkeypatch.setattr(compose, "call", rec)
    compose.reground("Data engineering role.", "Data Engineer", _sel(), {"p1": ["ETL"]})
    sent = _sent(rec.users[0])
    assert [b["gkey"] for b in sent] == ["p1"]
    assert sent[0]["banned_tokens"] == ["ETL"]
    # And the atoms come along, because the rewrite has to be built from them.
    assert "job-discovery pipeline" in json.dumps(sent[0]["atoms"])
    # The length budget too: a recovered bullet still has to fit the line it goes on.
    assert "length_target" in sent[0]


def test_the_prompt_says_the_dropped_line_is_usually_the_opener(engine, monkeypatch):
    """The instruction that makes this a recovery and not just a filter. Without it the
    model is free to return a narrower line that no longer introduces the entry."""
    rec = Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW))
    monkeypatch.setattr(compose, "call", rec)
    compose.reground("Data engineering role.", "Data Engineer", _sel(), {"p1": ["ETL"]})
    assert "OPENING bullet" in rec.systems[0]


def test_a_gkey_the_selection_does_not_hold_is_refused(engine, monkeypatch):
    """A dropped key that is not in the group map has no atoms to rewrite from, so there
    is nothing to ask and no call to spend."""
    rec = Recorder()  # raises if called at all
    monkeypatch.setattr(compose, "call", rec)
    assert compose.reground("jd", "Data Engineer", _sel(), {"nope": ["ETL"]}) == {}
    assert rec.systems == []


def test_a_bullet_for_a_key_that_was_not_dropped_is_discarded(engine, monkeypatch):
    """The model answers about the whole payload; only the keys actually dropped may be
    written back, or a re-ask could overwrite a bullet that was already grounded."""
    monkeypatch.setattr(compose, "call", Recorder(
        answer=_answer(p1=REGROUNDED_OVERVIEW, p2="Rewrote something nobody asked for.")))
    out = compose.reground("jd", "Data Engineer", _sel(), {"p1": ["ETL"]})
    assert out == {"p1": REGROUNDED_OVERVIEW}


# ── the recovery, end to end ─────────────────────────────────────────────────

def test_a_recovered_overview_is_restored_and_reported(engine, monkeypatch):
    """The regression itself: the project keeps the bullet that says what it is."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW)))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._recover_dropped(ctx, rt_run._gate(ctx, stage="rephrase"))
    assert bullets["p1"] == REGROUNDED_OVERVIEW
    assert any("recovered 1 dropped bullet(s)" in n for n in report.note_lines)
    # The original drop is still on the record. A recovery does not erase the fact that
    # the first attempt was ungrounded.
    assert any("dropped bullet 'p1'" in w for w in report.warnings)


def test_a_re_ask_that_is_still_ungrounded_is_dropped_again_and_named(engine, monkeypatch):
    """The safety property. The repair is not trusted: it goes through the same gate, and
    a second failure is as visible as the first rather than reading like a clean run."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(
        p1="Designed a job-discovery pipeline built on a Kafka backbone.")))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._recover_dropped(ctx, rt_run._gate(ctx, stage="rephrase"))
    assert "p1" not in bullets
    assert any("[reground] dropped bullet 'p1'" in w and "Kafka" in w
               for w in report.warnings)
    assert not any("recovered" in n for n in report.note_lines)


def test_a_transport_failure_leaves_the_drop_standing(engine, monkeypatch):
    """Advisory, never fatal: a re-ask that cannot run costs the run nothing beyond the
    bullet it had already lost."""
    monkeypatch.setattr(compose, "call", Recorder(raises=True))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._recover_dropped(ctx, rt_run._gate(ctx, stage="rephrase"))
    assert bullets == {"p2": DETAIL}
    assert any("stay dropped" in w for w in report.warnings)


def test_a_clean_run_never_spends_a_call(engine, monkeypatch):
    """The cost contract. Nothing dropped means nothing to recover, so the stage does not
    exist on a run whose bullets were all grounded."""
    rec = Recorder()  # raises if called at all
    monkeypatch.setattr(compose, "call", rec)
    bullets = {"p1": REGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    handled = rt_run._gate(ctx, stage="rephrase")
    assert handled == {}
    rt_run._recover_dropped(ctx, handled)
    assert rec.systems == []
    assert "reground" not in report.stages


def test_a_reverted_bullet_is_not_re_asked(engine, monkeypatch):
    """`handled` also carries bullets the gate REVERTED to grounded text. Those are
    already fixed; re-asking them would spend a call to replace good text."""
    rec = Recorder()  # raises if called at all
    monkeypatch.setattr(compose, "call", rec)
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    handled = rt_run._gate(ctx, stage="rephrase",
                           fallback={"p1": REGROUNDED_OVERVIEW})
    assert bullets["p1"] == REGROUNDED_OVERVIEW      # reverted, not dropped
    rt_run._recover_dropped(ctx, handled)
    assert rec.systems == []


# ── the toggle ───────────────────────────────────────────────────────────────

def test_the_toggle_defaults_on(engine):
    assert config.reground_enabled() is True


def test_the_env_variable_turns_it_off(engine, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_REGROUND", "0")
    assert config.reground_enabled() is False


def test_config_json_turns_it_off(engine, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"reground": False})
    assert config.reground_enabled() is False
