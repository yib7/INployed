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
# A second ungrounded bullet, for the partial-answer test: a re-ask needs two dropped
# bullets to show it answering one and not the other. "Kafka" is in neither atom.
UNGROUNDED_DETAIL = ("Configured a two-stage relevance scorer on a Kafka backbone with "
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
    """The regression itself: the project keeps the bullet that says what it is. A
    recovered drop is not a degraded run -- the resume that ships is fully grounded --
    so it must not warn; the record of the original drop lives in the notes instead,
    still on the record for anyone who reads the report, just not on the channel that
    marks a run degraded in the dialog."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW)))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert bullets["p1"] == REGROUNDED_OVERVIEW
    assert report.warnings == []
    # The original drop is still on the record -- just as a note, not a warning.
    assert any("[rephrase] dropped bullet 'p1'" in n and "ETL" in n
               for n in report.note_lines)
    assert any("recovered 1 dropped bullet(s)" in n for n in report.note_lines)


def test_two_drops_both_recovered_leave_no_warning(engine, monkeypatch):
    """The commonest shape in the 2026-09-15 batch, pinned from a real report: one run
    lost TWO bullets to the prologue gate, the re-ask brought both back, and the
    dashboard still counted the resume among the ones "with warnings". The single-drop
    test above pins the severity of one recovered drop; this one pins that the count of
    drops changes nothing. A run whose every drop was recovered ships a complete,
    grounded resume, so it warns zero times however many bullets the re-ask had to
    save, and the notes carry each drop plus one recovery line that names them all."""
    monkeypatch.setattr(compose, "call",
                        Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW, p2=DETAIL)))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": UNGROUNDED_DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert bullets == {"p1": REGROUNDED_OVERVIEW, "p2": DETAIL}
    assert report.warnings == []
    assert sum("[rephrase] dropped bullet" in n for n in report.note_lines) == 2
    assert any("recovered 2 dropped bullet(s) on a re-ask: p1, p2" in n
               for n in report.note_lines)


def test_a_still_ungrounded_re_ask_leaves_exactly_one_warning(engine, monkeypatch):
    """The other half of the same invariant: when the re-ask does NOT save the bullet,
    the loss still reads as exactly one warning -- the reground stage's, not the
    prologue's, since the prologue's drop was provisional and never printed as one."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(
        p1="Designed a job-discovery pipeline built on a Kafka backbone.")))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert "p1" not in bullets
    assert len(report.warnings) == 1
    assert "[reground] dropped bullet 'p1'" in report.warnings[0]
    assert "Kafka" in report.warnings[0]
    # The prologue's own drop line is in the notes, not the warnings.
    assert any("[rephrase] dropped bullet 'p1'" in n for n in report.note_lines)


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


def test_a_failed_re_ask_names_the_bullet_it_lost(engine, monkeypatch):
    """A transport failure still has to name what it lost: "1 bullet(s) stay dropped"
    on its own reads as one anonymous blob once a batch loses more than one. Naming
    the gkey and its ungrounded tokens on the same line as the failure is what makes
    the warning useful without opening the report."""
    monkeypatch.setattr(compose, "call", Recorder(raises=True))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert len(report.warnings) == 1
    w = report.warnings[0]
    assert "stay dropped" in w and "p1" in w and "ETL" in w


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


def test_a_re_ask_that_returns_nothing_names_the_bullet_it_lost(engine, monkeypatch):
    """The other silent-failure path: the model answers, but with no bullets at all.
    Same naming requirement as a transport failure -- the report has to say which
    bullet(s) stayed dropped, not just how many."""
    monkeypatch.setattr(compose, "call", Recorder(answer={"bullets": []}))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert len(report.warnings) == 1
    w = report.warnings[0]
    assert "returned nothing" in w and "p1" in w and "ETL" in w


def test_a_partially_answered_re_ask_names_the_bullet_it_did_not_answer(engine, monkeypatch):
    """The fourth outcome fix 1 closes: a re-ask that answers some of the dropped
    bullets but not every one of them. Before the fix `ctx.bullets.update` folded in
    the answered ones and the rest were simply absent, so the run said nothing about
    the loss. This pins the new warning, naming only the bullet left behind, and
    confirms the bullet that was recovered still gets its usual note."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW)))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": UNGROUNDED_DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert bullets["p1"] == REGROUNDED_OVERVIEW
    assert "p2" not in bullets
    assert len(report.warnings) == 1
    w = report.warnings[0]
    assert "[reground]" in w and "p2" in w and "Kafka" in w
    assert "p1 (ungrounded" not in w
    assert any("recovered 1 dropped bullet(s) on a re-ask: p1" in n
               for n in report.note_lines)


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


# ── drops_as_notes: severity, not existence ───────────────────────────────────

def test_with_reground_off_a_prologue_drop_is_still_a_warning(engine, monkeypatch):
    """`drops_as_notes` tracks whether a re-ask is actually coming, not just which
    stage is calling. With reground off there is no recovery to wait for, so the
    prologue's drop is the final word on the bullet and has to warn immediately, same
    as any other stage's drop."""
    monkeypatch.setenv("RESUME_TAILOR_REGROUND", "0")
    monkeypatch.setattr(compose, "call", Recorder())  # raises if called at all
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert "p1" not in bullets
    assert len(report.warnings) == 1
    assert "[rephrase] dropped bullet 'p1'" in report.warnings[0]
    assert not any("recovered" in n for n in report.note_lines)


def test_the_report_records_the_rejected_text(engine, monkeypatch):
    """The token name alone cannot tell a fabrication from a tokenizer false positive
    (the v1.4.0 -> 0 case). The rejected text, captured verbatim before the gate
    mutates `bullets`, is what makes that call possible after the run, from the
    report alone."""
    monkeypatch.setattr(compose, "call", Recorder(answer=_answer(p1=REGROUNDED_OVERVIEW)))
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._prologue_gate(ctx)
    assert any("[rephrase] rejected text for 'p1'" in n and "whose ETL flow" in n
               for n in report.note_lines)
    # Stable, fixed quoting: a JSON-style double quote sits right after the colon.
    # `!r`'s style flips with the text's own apostrophes; this one does not.
    line = next(n for n in report.note_lines if "[rephrase] rejected text for 'p1'" in n)
    assert ': "Designed' in line


def test_a_reverted_bullet_still_warns_even_when_drops_are_notes(engine):
    """`drops_as_notes` only ever downgrades a DROP. A REVERTED bullet means some
    pass produced ungrounded text outright and a fallback happened to exist -- there
    is no re-ask coming to excuse it -- so it must warn regardless of the flag."""
    bullets = {"p1": UNGROUNDED_OVERVIEW, "p2": DETAIL}
    report = rt_run.RunLog()
    ctx = _ctx(bullets, report)
    rt_run._gate(ctx, stage="style gate", fallback={"p1": REGROUNDED_OVERVIEW},
                 drops_as_notes=True)
    assert bullets["p1"] == REGROUNDED_OVERVIEW
    assert len(report.warnings) == 1
    assert "[style gate] reverted bullet 'p1'" in report.warnings[0]
    assert any("[style gate] rejected text for 'p1'" in n and "ETL" in n
               for n in report.note_lines)


# ── the toggle ───────────────────────────────────────────────────────────────

def test_the_toggle_defaults_on(engine):
    assert config.reground_enabled() is True


def test_the_env_variable_turns_it_off(engine, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_REGROUND", "0")
    assert config.reground_enabled() is False


def test_config_json_turns_it_off(engine, monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {"reground": False})
    assert config.reground_enabled() is False
