"""TL-4 as the run meets it: the faithfulness check after the rephrase and after
every later rewrite.

The deterministic grounding gate (`verify.enforce_grounded`) traces only
distinctive tokens, so a claim written in lowercase common words passes it: "Led
the team" over an atom that says the candidate helped carries nothing it can
check (`verify.py`'s docstring states the gap). With Jev on, TL-4 asks the judge
about every bullet against the atoms it was written from. A flagged bullet gets
one reground call with its finding named; still flagged, it reverts to its last
passing version, or is dropped exactly as the grounding gate drops one. TL-4 may
only reject, revert or drop a bullet; the reground call writes any new text, and
the grounding gate still runs as before.

Nothing here calls a model or Jev: `compose.call` is replaced in every test, and
every judge is a fake. The whole runs reuse the golden module's pinned engine,
whose stub raises on any prompt it does not know.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from resume_tailor import assets, compose, config, jev_assist, measure, verify  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

import test_tailor_golden as golden  # noqa: E402 - sibling test module, no pkg import
import test_tailor_jev as tailor_jev  # noqa: E402

# The golden module's fixtures, re-exported by assignment (see
# test_sweep_layout_invariant.py for why not `from ... import`).
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

PROJECT = "Harbor"

_ATOMS = {
    "h1": {"id": "h1", "_block": PROJECT,
           "what": "Helped the team move the billing service to a queue-based design"},
    "h2": {"id": "h2", "_block": PROJECT,
           "what": "Wrote 40 integration tests for the billing service"},
    "h3": {"id": "h3", "_block": PROJECT,
           "what": "Documented the queue-based billing design for the support team"},
}
_INFLATION = jev_assist.FINDINGS["inflates"]

# Harbor's bullets: h1 as the atom has it, and the inflation TL-4 exists to catch.
# Every token of both passes the grounding gate.
_H1 = "Helped the team move the billing service to a queue-based design."
_H1_LED = "Led the team that moved the billing service to a queue-based design."
_H2 = "Wrote 40 integration tests for the billing service."


def _sel():
    return {"experience": [], "leadership": [],
            "projects": [{"name": PROJECT, "groups": [["h1"], ["h2"]]}]}


class Recorder:
    """A `compose.call` replacement that keeps every prompt and answers `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.systems, self.users = [], []

    def __call__(self, system, user, tier, **kw):
        self.systems.append(system)
        self.users.append(user)
        return self.answer


@pytest.fixture()
def engine(monkeypatch):
    """Every input reground and the gate read, pinned. No user data, no config.json."""
    monkeypatch.delenv("RESUME_TAILOR_REGROUND", raising=False)
    monkeypatch.setattr(assets, "atoms_by_id", lambda: {k: dict(v) for k, v in _ATOMS.items()})
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)
    jev_assist.reset_usage()


def _sent(user):
    """The payload out of a reground user message, parsed."""
    body = user.split("REJECTED BULLETS", 1)[1].split("Return ONLY JSON", 1)[0]
    return json.loads(body[body.index("["):body.rindex("]") + 1])


def _reground(monkeypatch, dropped, **kw):
    rec = Recorder({"bullets": []})
    monkeypatch.setattr(compose, "call", rec)
    compose.reground("Platform role.", "Engineer", _sel(), dropped, **kw)
    return rec


# ── reground names the finding ───────────────────────────────────────────────
def test_a_finding_rides_with_its_bullet_and_is_explained_once(engine, monkeypatch):
    rec = _reground(monkeypatch, {"h1": [], "h2": ["Kafka"]}, findings={"h1": _INFLATION})
    sent = {b["gkey"]: b for b in _sent(rec.users[0])}
    assert sent["h1"]["finding"] == _INFLATION
    assert sent["h1"]["banned_tokens"] == []
    assert "finding" not in sent["h2"]
    (system,) = rec.systems
    assert system.count(compose.REGROUND_FINDING_RULE) == 1


def test_a_finding_carries_its_bullets_role_in_the_block(engine, monkeypatch):
    """The reground prompt's note on opening bullets would turn a flagged detail
    bullet into a second overview, so a bullet with a finding says which it is: the
    block's first bullet opens it, every other is a detail."""
    rec = _reground(monkeypatch, {"h1": [], "h2": []},
                    findings={"h1": _INFLATION, "h2": _INFLATION})
    sent = {b["gkey"]: b for b in _sent(rec.users[0])}
    assert (sent["h1"]["role"], sent["h2"]["role"]) == ("opening", "detail")


def test_only_a_bullet_with_a_finding_carries_a_role(engine, monkeypatch):
    rec = _reground(monkeypatch, {"h1": ["Kafka"], "h2": []}, findings={"h2": _INFLATION})
    sent = {b["gkey"]: b for b in _sent(rec.users[0])}
    assert "role" not in sent["h1"] and sent["h2"]["role"] == "detail"


def test_the_finding_rule_tells_the_model_to_keep_the_bullets_role():
    rule = compose.REGROUND_FINDING_RULE
    assert "'role'" in rule
    assert "opening" in rule and "detail" in rule


@pytest.mark.parametrize("findings", [None, {}, {"h1": ""}], ids=["none", "empty", "passing"])
def test_without_a_finding_the_prompt_is_todays(engine, monkeypatch, findings):
    """No finding, no change: the Jev-off prompt stays word for word what it was,
    which `test_tailor_jev.py`'s recording pins against the engine before TL-4."""
    today = _reground(monkeypatch, {"h1": ["Kafka"]})
    got = _reground(monkeypatch, {"h1": ["Kafka"]}, findings=findings)
    assert (got.systems, got.users) == (today.systems, today.users)
    assert compose.REGROUND_FINDING_RULE not in got.systems[0]
    assert "finding" not in _sent(got.users[0])[0]


def test_the_finding_rule_is_free_of_the_banned_phrasing():
    rule = compose.REGROUND_FINDING_RULE
    assert compose.style_violations(rule) == []
    assert chr(0x2014) not in rule


# ── the check in the pass driver (run._check_faithfulness) ───────────────────
class Flagger:
    """A TL-4 judge: a bullet opening with one of `prefixes` inflates (0.95) and every
    other answer passes. Keeps each request's bullets; with `answers` set, the
    service goes down after that many requests."""

    def __init__(self, *prefixes, answers=None):
        self.prefixes = tuple(p.lower() for p in prefixes)
        self.answers = answers
        self.requests = []

    def judge(self, state, questions):
        if self.answers is not None and len(self.requests) >= self.answers:
            raise RuntimeError("the judge is down")
        self.requests.append(list(state["bullets"]))
        out = {}
        for i, text in enumerate(state["bullets"]):
            flag = text.lower().startswith(self.prefixes)
            out[f"supported_{i}"] = jev.Answer(
                kind="choice", choice="verified", confidence=0.9,
                probabilities={"verified": 0.9, "unsupported": 0.05, "contradicted": 0.05})
            out[f"inflates_{i}"] = jev.Answer(kind="noul", noul=0.95 if flag else 0.05)
            out[f"adds_claim_{i}"] = jev.Answer(kind="noul", noul=0.05)
        return out


def _ctx(bullets, judge, sel=None, reserved=frozenset()):
    return rt_run.PassCtx(jd="Platform role.", job_title="Engineer",
                          sel=sel if sel is not None else _sel(), bullets=bullets,
                          verbatim={}, reserved=reserved, log=lambda _m: None,
                          report=rt_run.RunLog(), judge=judge)


def _reask(monkeypatch, answers):
    """The reground call's transport, answering `answers` ({gkey: text})."""
    rec = Recorder({"bullets": [{"gkey": gk, "text": t} for gk, t in answers.items()]})
    monkeypatch.setattr(compose, "call", rec)
    return rec


def _check(ctx, stage="rephrase", **kw):
    return rt_run._check_faithfulness(ctx, stage=stage, **kw)


def _notes(ctx):
    return "\n".join(ctx.report.note_lines)


def test_with_jev_off_the_check_does_nothing(engine, monkeypatch):
    rec = _reask(monkeypatch, {})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=None)
    assert _check(ctx) == {}
    assert ctx.bullets == {"h1": _H1_LED, "h2": _H2}
    assert rec.users == []
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["requests"] == 0


def test_a_passing_entry_is_one_request_and_changes_nothing(engine, monkeypatch):
    rec = _reask(monkeypatch, {})
    judge = Flagger()
    ctx = _ctx({"h1": _H1, "h2": _H2}, judge=judge)
    assert _check(ctx) == {}
    assert judge.requests == [[_H1, _H2]]
    assert ctx.bullets == {"h1": _H1, "h2": _H2}
    assert rec.users == []
    assert ctx.report.stages == [] and ctx.report.notes == [] and ctx.report.entries == []


def test_a_flagged_bullet_is_regrounded_once_with_its_finding(engine, monkeypatch):
    rec = _reask(monkeypatch, {"h1": _H1})
    judge = Flagger("led")
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=judge)
    assert _check(ctx) == {"h1": _INFLATION}
    assert ctx.bullets == {"h1": _H1, "h2": _H2}
    (user,) = rec.users
    (item,) = _sent(user)
    assert (item["gkey"], item["finding"], item["banned_tokens"]) == ("h1", _INFLATION, [])
    assert compose.REGROUND_FINDING_RULE in rec.systems[0]
    # the entry, then the re-check of the regrounded text alone
    assert judge.requests == [[_H1_LED, _H2], [_H1]]
    assert ctx.report.entries == []
    assert ctx.report.stages == [rt_run.FAITHFULNESS_REGROUND_STAGE]
    assert ctx.report.note_lines == [
        f"grounding: [rephrase] faithfulness: flagged text for 'h1' ({_INFLATION}): "
        f"{json.dumps(_H1_LED)}",
        f"grounding: [rephrase] faithfulness: regrounded bullet 'h1' ({_INFLATION})"]


def test_one_reground_call_serves_every_flagged_bullet(engine, monkeypatch):
    rec = _reask(monkeypatch, {"h1": _H1, "h2": _H2})
    h2_led = "Led 40 integration tests for the billing service."
    judge = Flagger("led")
    ctx = _ctx({"h1": _H1_LED, "h2": h2_led}, judge=judge)
    _check(ctx)
    assert len(rec.users) == 1
    assert {i["gkey"]: i["finding"] for i in _sent(rec.users[0])} == {
        "h1": _INFLATION, "h2": _INFLATION}
    assert ctx.bullets == {"h1": _H1, "h2": _H2}
    assert judge.requests == [[_H1_LED, h2_led], [_H1, _H2]]


def test_still_flagged_at_the_prologue_it_is_dropped_as_the_gate_drops(engine, monkeypatch):
    """No earlier text to go back to: the bullet goes, with a warning naming the
    finding and why the re-ask did not save it, and notes holding both texts."""
    again = "Led the move of the billing service to a queue-based design."
    _reask(monkeypatch, {"h1": again})
    judge = Flagger("led")
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=judge)
    _check(ctx)
    assert ctx.bullets == {"h2": _H2}
    assert judge.requests == [[_H1_LED, _H2], [again]]
    assert ctx.report.warnings == [
        f"grounding: [rephrase] faithfulness: dropped bullet 'h1' ({_INFLATION}; "
        f"the re-check still flags it: {_INFLATION})"]
    assert f"flagged text for 'h1' ({_INFLATION}): {json.dumps(_H1_LED)}" in _notes(ctx)
    assert f"refused the re-ask's text for 'h1': {json.dumps(again)}" in _notes(ctx)


def test_still_flagged_after_a_pass_it_reverts_to_the_snapshot(engine, monkeypatch):
    """The dedupe swapped h1's opener for one its atom does not back, and the re-ask
    returns nothing: h1 goes back to its text from before the pass."""
    _reask(monkeypatch, {})
    snapshot = {"h1": _H1, "h2": _H2}
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.VERB_DEDUPE_STAGE, snapshot=snapshot)
    assert ctx.bullets == snapshot
    assert ctx.report.warnings == [
        f"grounding: [verb dedupe] faithfulness: reverted bullet 'h1' ({_INFLATION}; "
        "the re-ask returned nothing)"]


def test_only_the_bullets_a_pass_changed_are_asked(engine, monkeypatch):
    """h2 is as the pass found it, and h1's only change is the style gate's em-dash
    strip: there is nothing to ask, so the judge is never called."""
    dashed = "Helped the team " + chr(0x2014) + " moving billing to a queue-based design."
    judge = Flagger("helped")    # it would flag h1 if asked
    ctx = _ctx({"h1": compose._strip_em_dashes(dashed), "h2": _H2}, judge=judge)
    got = _check(ctx, stage=rt_run.STYLE_GATE_STAGE, snapshot={"h1": dashed, "h2": _H2},
                 retrim=True)
    assert got == {} and judge.requests == []
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["note"] == jev_assist.NOTHING_TO_ASK


def test_a_revert_at_the_style_gate_keeps_the_gates_em_dash_strip(engine, monkeypatch):
    """The style gate's snapshot predates the gate, and nothing after the gate strips
    an em dash, so the revert takes the strip the gate gives every bullet."""
    _reask(monkeypatch, {})
    dashed = "Helped the team " + chr(0x2014) + " moving billing to a queue-based design."
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.STYLE_GATE_STAGE, snapshot={"h1": dashed, "h2": _H2},
           retrim=True)
    assert ctx.bullets["h1"] == compose._strip_em_dashes(dashed)
    assert chr(0x2014) not in ctx.bullets["h1"]


def test_a_flagged_fill_is_undone_onto_its_old_key(engine, monkeypatch):
    """The underfull fill moved h1 onto h1+h3, a key the snapshot does not hold: the
    fill is undone, so h1 has its old text and h3 is a spare atom again."""
    _reask(monkeypatch, {})
    sel = _sel()
    sel["projects"][0]["groups"] = [["h1", "h3"], ["h2"]]
    filled = "Led the team that moved billing to a queue-based design and documented it."
    ctx = _ctx({"h2": _H2, "h1+h3": filled}, judge=Flagger("led"), sel=sel)
    _check(ctx, stage="underfull fill", snapshot={"h1": _H1, "h2": _H2}, retrim=True)
    assert ctx.bullets == {"h2": _H2, "h1": _H1}
    assert sel["projects"][0]["groups"] == [["h1"], ["h2"]]
    assert ctx.report.warnings == [
        f"grounding: [underfull fill] faithfulness: reverted bullet 'h1+h3' ({_INFLATION}; "
        "the re-ask returned nothing)"]


@pytest.mark.parametrize("text, why", [
    ("Helped the team move billing to Kafka and a queue-based design.",
     "the re-ask's text is ungrounded: Kafka"),
    ("Wrote the queue-based billing design with the team.",
     "the re-ask's text opens with 'wrote', which another bullet already uses"),
    (_H1_LED, "the re-ask returned the same text"),
], ids=["ungrounded", "opener", "same-text"])
def test_a_reground_the_code_refuses_never_reaches_the_recheck(engine, monkeypatch, text,
                                                                why):
    _reask(monkeypatch, {"h1": text})
    judge = Flagger("led")
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=judge)
    _check(ctx, stage="underfull fill", snapshot={"h1": _H1, "h2": _H2}, retrim=True)
    assert judge.requests == [[_H1_LED]]         # h2 is unchanged, so it is not asked
    assert ctx.bullets["h1"] == _H1
    assert ctx.report.warnings == [
        f"grounding: [underfull fill] faithfulness: reverted bullet 'h1' ({_INFLATION}; {why})"]


# A palette where "wrote" and "led" share a category, so the swap's home category
# holds the verb Jev flagged and one fresh verb.
_PALETTE = {"Building": ["Wrote", "Led", "Authored"], "Helping": ["Supported"]}


def test_at_the_verb_dedupe_a_shared_opener_is_no_reason_to_revert(engine, monkeypatch):
    """The dedupe's own revert target is the text it had to change, whose opener
    collided already, so only the later passes refuse a reground for its opener. At
    the dedupe a shared opener on the regrounded text gets the dedupe's in-category
    swap, never the verb Jev flagged."""
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in _PALETTE.items()})
    shared = "Wrote the queue-based billing design with the team."
    _reask(monkeypatch, {"h1": shared})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.VERB_DEDUPE_STAGE, snapshot={"h1": _H1, "h2": _H2})
    assert ctx.bullets["h1"] == "Authored the queue-based billing design with the team."
    assert ctx.report.warnings == []


_WROTE_H1 = "Wrote the queue-based billing design with the team."


def test_a_revert_at_the_verb_dedupe_gets_a_fresh_opener(engine, monkeypatch):
    """The dedupe rewrote h1 because its opener repeated h2's, so the revert target
    repeats it too. The revert takes the dedupe's in-category swap, skipping the verb
    Jev flagged."""
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in _PALETTE.items()})
    _reask(monkeypatch, {})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.VERB_DEDUPE_STAGE, snapshot={"h1": _WROTE_H1, "h2": _H2})
    assert ctx.bullets == {"h1": "Authored the queue-based billing design with the team.",
                           "h2": _H2}
    assert ctx.report.warnings == [
        f"grounding: [verb dedupe] faithfulness: reverted bullet 'h1' ({_INFLATION}; "
        "the re-ask returned nothing)"]


def test_a_revert_at_the_verb_dedupe_with_no_fresh_verb_names_the_repeat(engine, monkeypatch):
    """With every palette verb taken, the revert keeps the faithful text and its
    warning says the opener repeats."""
    monkeypatch.setattr(assets, "active_verbs", lambda: {"Building": ["Wrote", "Led"]})
    _reask(monkeypatch, {})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.VERB_DEDUPE_STAGE, snapshot={"h1": _WROTE_H1, "h2": _H2})
    assert ctx.bullets == {"h1": _WROTE_H1, "h2": _H2}
    assert ctx.report.warnings == [
        f"grounding: [verb dedupe] faithfulness: reverted bullet 'h1' ({_INFLATION}; "
        "the re-ask returned nothing; repeated opener)"]


def test_a_revert_after_the_verb_dedupe_keeps_its_opener(engine, monkeypatch):
    """Only the dedupe's revert target is known to repeat an opener; a later pass's
    snapshot has the dedupe's unique openers, so its revert is left as it was."""
    monkeypatch.setattr(assets, "active_verbs", lambda: {k: list(v) for k, v in _PALETTE.items()})
    _reask(monkeypatch, {})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage="underfull fill", snapshot={"h1": _WROTE_H1, "h2": _H2}, retrim=True)
    assert ctx.bullets["h1"] == _WROTE_H1


def test_after_the_style_gate_a_reground_may_add_no_style_finding(engine, monkeypatch):
    _reask(monkeypatch, {"h1": "Helped the team seamlessly move the billing service to a "
                               "queue-based design."})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=rt_run.AIWRITING_SWEEP_STAGE, snapshot={"h1": _H1, "h2": _H2},
           retrim=True)
    assert ctx.bullets["h1"] == _H1
    (warning,) = ctx.report.warnings
    assert "the re-ask's text adds hollow intensifier" in warning


_H1_LONG = ("Helped the team move the billing service to a queue-based design, one "
            "service at a time with the team, until the billing service ran on the "
            "queue-based design.")


@pytest.mark.parametrize("stage", [rt_run.AIWRITING_SWEEP_STAGE, rt_run.STYLE_GATE_STAGE])
def test_at_the_sweep_a_reground_may_not_outgrow_the_bullet(engine, monkeypatch, stage):
    """The sweep keeps the pass's printed lines from growing, and nothing after it
    trims, so at the sweep a regrounded text longer than the bullet's pre-sweep text
    is refused and the bullet reverts. The style gate's line budget is the target the
    re-trim enforces, so there the same text stands."""
    assert measure.line_count(_H1) == 1 and measure.line_count(_H1_LONG) == 2
    _reask(monkeypatch, {"h1": _H1_LONG})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=stage, snapshot={"h1": _H1, "h2": _H2}, retrim=True)
    if stage == rt_run.STYLE_GATE_STAGE:
        assert ctx.bullets["h1"] == _H1_LONG and ctx.report.warnings == []
    else:
        assert ctx.bullets["h1"] == _H1
        assert ctx.report.warnings == [
            f"grounding: [{stage}] faithfulness: reverted bullet 'h1' ({_INFLATION}; "
            "the re-ask's text runs to 2 printed lines, past the 1 the bullet had "
            "before the sweep)"]


def test_a_recheck_that_cannot_run_counts_as_flagged(engine, monkeypatch):
    _reask(monkeypatch, {"h1": _H1})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led", answers=1))
    _check(ctx)
    assert ctx.bullets == {"h2": _H2}
    assert ctx.report.warnings == [
        f"grounding: [rephrase] faithfulness: dropped bullet 'h1' ({_INFLATION}; "
        "the re-check could not run)"]
    assert jev_assist.usage(jev_assist.STEP_FAITHFULNESS)["note"] == (
        "fell back to the deterministic gate alone (RuntimeError)")


_LONG = ("Helped the team move the billing service to a queue-based design, and worked "
         "through the retry paths, the dead letter handling and the replay tooling with "
         "the rest of the team over the whole of the quarter, one service at a time, "
         "until every consumer read from the queue and the old polling jobs were gone "
         "for good.")


@pytest.mark.parametrize("stage, retrim", [(rt_run.VERB_DEDUPE_STAGE, False),
                                           ("underfull fill", True)])
def test_a_reground_is_trimmed_where_its_pass_re_trims(engine, monkeypatch, stage, retrim):
    """At the dedupe the verbatim + trim pass comes next and trims every bullet; a
    pass that re-trims its own output trims the regrounded text the same way."""
    assert measure.line_count(_LONG) > 2
    _reask(monkeypatch, {"h1": _LONG})
    ctx = _ctx({"h1": _H1_LED, "h2": _H2}, judge=Flagger("led"))
    _check(ctx, stage=stage, snapshot={"h1": _H1, "h2": _H2}, retrim=retrim)
    got = ctx.bullets["h1"]
    assert got == (rt_run._fit_to_lines(_LONG, 2) if retrim else _LONG)
    assert measure.line_count(got) <= 2 or not retrim


# ── the planted inflation, end to end ─────────────────────────────────────────
# The golden master's rc_lead atom, and the same atom saying the candidate HELPED.
_RC_ATOM = "Led a team of 9 students to a regional finals placement"
_RC_HELPED = "Helped a team of 9 students reach a regional finals placement"
_RC_LED = _RC_ATOM + "."            # what the golden rephrase writes for rc_lead
_RC_REGROUNDED = _RC_HELPED + "."


class InflationReader:
    """FakeJev, except that it reads an inflation the way a live judge would: a bullet
    opening "Led" over atoms that say the candidate helped inflates (0.95)."""

    def __init__(self):
        self.fake = jev.FakeJev()
        self.caught = []

    def judge(self, state, questions):
        out = self.fake.judge(state, questions)
        for qid in questions:
            if not qid.startswith("inflates_"):
                continue
            i = int(qid.rsplit("_", 1)[1])
            text = state["bullets"][i]
            if (text.lower().startswith("led ")
                    and "helped" in json.dumps(state["atoms"][i]).lower()):
                out[qid] = jev.Answer(kind="noul", noul=0.95)
                self.caught.append(text)
        return out


@pytest.fixture()
def planted(pinned_engine, stub_template_head):
    """The golden engine, with rc_lead's atom saying the candidate helped the team.
    The rephrase stub still writes the golden's "Led a team ..."."""
    master = Path(config.MASTER_YAML)
    text = master.read_text(encoding="utf-8")
    assert text.count(_RC_ATOM) == 1
    master.write_text(text.replace(_RC_ATOM, _RC_HELPED), encoding="utf-8")
    for fn in golden._CACHED:
        fn.cache_clear()
    return pinned_engine


def _golden_with_reground(monkeypatch, stages, answer):
    """The golden stub, answering the reground call with `answer` for each bullet it
    is sent. Returns the (system, user) prompt of every reground call."""
    stub = golden._make_stub(stages)
    regrounds = []

    def _call(system, user, tier, **kw):
        if "You repair resume bullets that failed a grounding check" in system:
            stages.append("reground")
            regrounds.append((system, user))
            return {"bullets": [{"gkey": b["gkey"], "text": answer} for b in _sent(user)]}
        return stub(system, user, tier, **kw)

    golden._install_stub(monkeypatch, _call)
    return regrounds


def test_a_planted_led_the_team_inflation_is_caught(planted, tmp_path, monkeypatch):
    """Nothing in "Led a team of 9 students ..." is a distinctive token the helped
    atom lacks, so the grounding gate passes it. TL-4 flags it, one reground call
    names the finding, and the regrounded text is the one that ships."""
    assert verify.unseen_tokens(_RC_LED, verify.group_source_text(["rc_lead"])) == []
    regrounds = _golden_with_reground(monkeypatch, planted, _RC_REGROUNDED)
    reader = InflationReader()
    tailor_jev._jev_on(monkeypatch, reader)
    captured = tailor_jev._run_tailor(monkeypatch, tmp_path)
    assert reader.caught == [_RC_LED]
    ((system, user),) = regrounds
    (item,) = _sent(user)
    assert (item["gkey"], item["finding"]) == ("rc_lead", _INFLATION)
    assert compose.REGROUND_FINDING_RULE in system
    assert captured["bullets"]["rc_lead"] == _RC_REGROUNDED
    report = tailor_jev._report(tmp_path)
    assert "warnings (0)" in report
    assert f"[rephrase] faithfulness: regrounded bullet 'rc_lead' ({_INFLATION})" in report


def test_a_planted_inflation_the_reground_repeats_is_dropped(planted, tmp_path, monkeypatch):
    again = "Led 9 students to a regional finals placement."
    regrounds = _golden_with_reground(monkeypatch, planted, again)
    reader = InflationReader()
    tailor_jev._jev_on(monkeypatch, reader)
    captured = tailor_jev._run_tailor(monkeypatch, tmp_path)
    assert reader.caught == [_RC_LED, again]
    assert len(regrounds) == 1
    assert "rc_lead" not in captured["bullets"]
    report = tailor_jev._report(tmp_path)
    assert (f"grounding: [rephrase] faithfulness: dropped bullet 'rc_lead' ({_INFLATION}; "
            f"the re-check still flags it: {_INFLATION})") in report
    assert f"flagged text for 'rc_lead' ({_INFLATION}): {json.dumps(_RC_LED)}" in report
    assert f"refused the re-ask's text for 'rc_lead': {json.dumps(again)}" in report
