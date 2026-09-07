"""The WIRING of the item-level AI-writing sweep: the pass, the toggle, the report.

`tests/test_item_sweep.py` covers what `sweep.sweep_items` decides. This file covers
what `run.py` does with it, which is a different set of mistakes:

* the pass is registered, and registered LAST, so the judgment call reads text the
  deterministic style gate has already cleaned and nothing downstream re-lengthens it;
* the toggle is held as a FUNCTION on the `Pass`, so a config change takes effect on the
  next run instead of at the next dashboard restart;
* off means off: zero calls and not one bullet touched;
* the run report carries what the sweep did, what it refused, and the P2 findings it
  left alone by policy, at the right severity;
* the pass's `retrim` is the no-op backstop it is documented to be.

**Nothing here calls a model.** `sweep.call` is monkeypatched in every test that reaches
the sweep, and the toggle-off tests install a stub that raises if it is called at all.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import assets, config, itemcheck, measure, sweep  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

ITEM = "Example Corp"

# Deliberately digit-free and capital-free past the opening verb: `verify.enforce_grounded`
# traces numbers and capitalized/inner-case tokens back to a group's atoms and skips the
# opening verb slot, so bullets written this way give the grounding gate nothing to act on
# and every assertion below observes the sweep alone.
_ATOMS = {
    "b1": {"id": "b1", "what": "dashboards for three internal teams"},
    "b2": {"id": "b2", "what": "the stages of the ingestion pipeline"},
    "b3": {"id": "b3", "what": "the nightly job schedule"},
}

# Two three-part series in one item, which is `rule_of_three` -- a P2 finding, reported
# and never repaired. `test_the_item_carries_exactly_one_p2_finding_and_no_p1` pins that
# reading, so the report assertions cannot pass against a differently-shaped item.
BULLETS = {
    "b1": "Built dashboards for the finance, operations, and support teams.",
    "b2": "Designed the ingestion, validation, and export stages of the pipeline.",
    "b3": "Rewrote the nightly job so it finishes before the morning standup.",
}

# Accepted: same opening verb, every original token still present, still one line.
_REPAIRED_B1 = ("Built dashboards for the finance, operations, and support teams every "
                "quarter.")
# Refused for `sweep.REASON_VERB`: a swapped opener would break the distinct-openers
# guarantee `compose.dedupe_leading_verbs` already established. Chosen over an over-long
# rewrite on purpose, because a verb rejection is final and does not buy the bounded
# re-ask, so this item stays at exactly one call.
_REFUSED_B2 = "Created the ingestion, validation, and export stages of the pipeline."


def _sel():
    return {"experience": [{"name": ITEM, "groups": [["b1"], ["b2"], ["b3"]]}],
            "projects": [], "leadership": []}


def _answer(**texts):
    return {"bullets": [{"gkey": gk, "text": t} for gk, t in texts.items()]}


class Recorder:
    """A `sweep.call` replacement. `answer` is returned to the item call; a re-ask or a
    second item call has nowhere to go and raises, so "the toggle is off" and "no re-ask
    fired" are enforced by the stub rather than only by an assertion after the fact."""

    def __init__(self, answer=None, raises=False):
        self.answer, self.raises = answer, raises
        self.systems = []

    def __call__(self, system, user, tier, **kw):
        self.systems.append(system)
        if self.raises:
            raise RuntimeError("transport down")
        if self.answer is None:
            raise AssertionError("the sweep issued a call it should not have")
        return self.answer


@pytest.fixture()
def engine(monkeypatch):
    """Every input the sweep and the pass read, pinned. No user data, no config.json."""
    monkeypatch.delenv("RESUME_TAILOR_AIWRITING_SWEEP", raising=False)
    monkeypatch.setattr(assets, "atoms_by_id", lambda: {k: dict(v)
                                                        for k, v in _ATOMS.items()})
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)


def _drive(monkeypatch, bullets, *, on_warning=None):
    """Run the REAL sweep `Pass` through the REAL driver.

    Not a hand-rolled copy of the pass loop: every defect this file is about lives in the
    WIRING, so the test has to go through `_run_bullet_passes` and the `Pass` object
    `run.py` declares. Returns the `RunLog` the driver wrote into.
    """
    report = rt_run.RunLog(on_warning=on_warning)
    ctx = rt_run.PassCtx(jd="Data engineering role.", job_title="Data Engineer",
                         sel=_sel(), bullets=bullets, verbatim={},
                         reserved=frozenset(), log=lambda _m: None, report=report)
    sweep_pass = next(p for p in rt_run._BULLET_PASSES
                      if p.name == rt_run.AIWRITING_SWEEP_STAGE)
    rt_run._run_bullet_passes(ctx, passes=(sweep_pass,))
    return report


# ── the fixture has to be honest ─────────────────────────────────────────────
def test_the_bullets_fit_their_own_budget(engine):
    """Every assertion here rests on this: the starting text is already legal, so a
    re-trim that changes anything is the backstop firing, never the setup."""
    for text in list(BULLETS.values()) + [_REPAIRED_B1, _REFUSED_B2]:
        assert measure.line_count(text) <= 2


def test_the_item_carries_exactly_one_p2_finding_and_no_p1(engine):
    """The report assertions read "one P2, nothing repairable" off this item, so pin it.
    Without this the P2 line could be missing for the honest reason that there was no P2
    finding to print."""
    found = itemcheck.item_findings(ITEM, list(BULLETS.items()))
    assert [(f.detector, f.tier) for f in found] == [("rule_of_three", itemcheck.P2)]


# ── the pass is registered, last ─────────────────────────────────────────────
def test_the_sweep_is_the_last_bullet_pass():
    """Order is the feature. The deterministic gate runs first so the one judgment call
    per item is spent on the structural tells only it can see, and nothing runs after the
    sweep, because the sweep's line-budget acceptance check is what keeps the printed
    line count non-increasing and a later stage could re-lengthen a bullet."""
    names = [p.name for p in rt_run._BULLET_PASSES]
    assert names[-1] == rt_run.AIWRITING_SWEEP_STAGE
    assert names[-2] == "style gate"


def test_the_pass_declares_its_bracketing():
    """`retrim` is the documented no-op backstop, `verify` re-runs the grounding gate in
    the direction the sweep's own acceptance check does not cover, and `recheck_fill`
    stays off because this pass never lengthens anything."""
    sweep_pass = rt_run._BULLET_PASSES[-1]
    assert sweep_pass.run is rt_run._pass_aiwriting_sweep
    assert (sweep_pass.retrim, sweep_pass.verify, sweep_pass.recheck_fill) == (
        True, True, False)


def test_the_pass_holds_the_toggle_function_not_its_value():
    """A `Pass` storing `config.aiwriting_sweep_enabled()` would freeze the answer at
    import; storing the function is what makes the setting live.

    Matched by name rather than by identity: other tests in the suite reload
    `resume_tailor.config`, so `is` against this module's copy of the function passes
    alone and fails in a full run. `test_the_toggle_is_read_per_run_not_at_import`
    is the behavioural half of this and does not care which copy is bound.
    """
    enabled = rt_run._BULLET_PASSES[-1].enabled
    assert callable(enabled)
    assert enabled.__name__ == "aiwriting_sweep_enabled"
    assert enabled.__module__.endswith("resume_tailor.config")


# ── the toggle ───────────────────────────────────────────────────────────────
def test_the_toggle_defaults_on(engine):
    assert config.aiwriting_sweep_enabled() is True


def test_config_json_can_turn_it_off(engine, monkeypatch):
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_aiwriting_sweep": False})
    assert config.aiwriting_sweep_enabled() is False


def test_env_turns_it_off(engine, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_AIWRITING_SWEEP", "0")
    assert config.aiwriting_sweep_enabled() is False


def test_env_beats_a_config_that_turned_it_off(engine, monkeypatch):
    """env wins both ways, matching every other toggle in `config.py`."""
    monkeypatch.setenv("RESUME_TAILOR_AIWRITING_SWEEP", "1")
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_aiwriting_sweep": False})
    assert config.aiwriting_sweep_enabled() is True


def test_the_settings_schema_exposes_the_toggle_defaulting_on():
    """The unified config GUI is where a user turns a billed stage off, so the row has
    to exist and its help has to name the cost. The default is ON by decision, over
    cheaper gating, which is exactly why the price is spelled out on the checkbox."""
    import settings  # noqa: PLC0415 - imported here so the sys.path insert applies

    field = next(f for f in settings.SETTINGS_SCHEMA
                 if f.key == "resume_aiwriting_sweep")
    assert (field.type, field.default, field.section, field.target) == (
        "bool", True, "Resume", "config")
    assert "ONE MODEL CALL PER RÉSUMÉ ENTRY ON EVERY TAILOR RUN" in field.help


def test_the_env_example_documents_the_cost_too():
    """The other place someone meets this setting. `.env.example` is what a fresh clone
    copies to `.env`, and a per-run price that is only visible in the GUI is invisible to
    anyone configuring the tool from the file."""
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if "RESUME_TAILOR_AIWRITING_SWEEP" in ln)
    assert line.startswith("# RESUME_TAILOR_AIWRITING_SWEEP=1")
    assert "ONE MODEL CALL PER RESUME ENTRY ON EVERY TAILOR RUN" in text


def test_a_blank_env_falls_through_to_config(engine, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_AIWRITING_SWEEP", "   ")
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_aiwriting_sweep": False})
    assert config.aiwriting_sweep_enabled() is False


def test_the_toggle_is_read_per_run_not_at_import(engine, monkeypatch):
    """Flip it BETWEEN two runs of the same registered `Pass` and watch the behaviour
    change. A value read at import would make the second run behave like the first, and
    the user would have to relaunch the dashboard to turn a billed stage off."""
    stored = {"resume_aiwriting_sweep": False}
    monkeypatch.setattr(config, "_config_json", lambda: dict(stored))
    rec = Recorder(answer=_answer(**BULLETS))
    monkeypatch.setattr(sweep, "call", rec)

    off_bullets = dict(BULLETS)
    off_report = _drive(monkeypatch, off_bullets)
    assert rec.systems == []
    assert off_report.stages == []

    stored["resume_aiwriting_sweep"] = True
    on_bullets = dict(BULLETS)
    on_report = _drive(monkeypatch, on_bullets)
    assert len(rec.systems) == 1
    assert on_report.stages == [rt_run.AIWRITING_SWEEP_STAGE]


def test_a_disabled_sweep_makes_zero_calls_and_touches_no_bullet(engine, monkeypatch):
    """The cost promise. Off means the model is never reached, which the stub enforces
    by raising, and every bullet ships byte-identical."""
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"resume_aiwriting_sweep": False})
    rec = Recorder(answer=None)
    monkeypatch.setattr(sweep, "call", rec)

    bullets = dict(BULLETS)
    report = _drive(monkeypatch, bullets)

    assert rec.systems == []
    assert bullets == BULLETS
    assert report.stages == [] and report.notes == [] and report.entries == []


# ── the report ───────────────────────────────────────────────────────────────
@pytest.fixture()
def swept(engine, monkeypatch):
    """One run of the real pass over the item: one committed rewrite, one refusal, one
    bullet echoed back, one P2 finding left standing. Returns (bullets, report)."""
    monkeypatch.setattr(sweep, "call", Recorder(answer=_answer(
        b1=_REPAIRED_B1, b2=_REFUSED_B2, b3=BULLETS["b3"])))
    bullets = dict(BULLETS)
    return bullets, _drive(monkeypatch, bullets)


def test_the_committed_rewrite_is_the_only_bullet_that_moved(swept):
    bullets, _report = swept
    assert bullets == {**BULLETS, "b1": _REPAIRED_B1}


def test_the_report_carries_the_changed_count(swept):
    _bullets, report = swept
    line = f"{rt_run.KIND_AIWRITING}: [{rt_run.AIWRITING_SWEEP_STAGE}] swept 1 item(s)"
    summary = [n for n in report.note_lines if n.startswith(line)]
    assert len(summary) == 1
    assert "rewrote 1 bullet(s)" in summary[0]
    assert "no item needed the bounded re-ask" in summary[0]


def test_the_report_names_the_rejection_and_its_reason(swept):
    _bullets, report = swept
    hits = [n for n in report.note_lines if "kept the original of bullet 'b2'" in n]
    assert len(hits) == 1
    assert f"({sweep.REASON_VERB}:" in hits[0]
    assert f"'{ITEM}'" in hits[0]


def test_the_report_names_the_p2_finding_it_chose_not_to_fix(swept):
    """The frozen strictness answer is "repair P0 and P1, report P2", so a P2 line has to
    read as a decision rather than as a miss."""
    _bullets, report = swept
    hits = [n for n in report.note_lines if "rule_of_three" in n]
    assert len(hits) == 1
    assert "left in place by policy" in hits[0]
    assert "repairs P0 and P1 only" in hits[0]


def test_nothing_the_sweep_reports_marks_the_run_degraded(engine, monkeypatch):
    """`on_warning` is the dashboard's degraded-run channel. A refused rewrite keeps a
    bullet that was already grounded, clean and fitting, and a P2 finding is a policy, so
    neither belongs on it."""
    monkeypatch.setattr(sweep, "call", Recorder(answer=_answer(
        b1=_REPAIRED_B1, b2=_REFUSED_B2, b3=BULLETS["b3"])))
    collected: list[str] = []
    report = _drive(monkeypatch, dict(BULLETS), on_warning=collected.append)

    assert report.notes and report.entries == []
    assert collected == []


def test_a_failed_item_call_is_a_warning_and_the_bullets_survive(engine, monkeypatch):
    """The one thing here that IS a degradation: a stage the run paid for did not happen
    for that item. Same severity as a skipped ATS report or a failed cover letter."""
    monkeypatch.setattr(sweep, "call", Recorder(raises=True))
    collected: list[str] = []
    bullets = dict(BULLETS)
    report = _drive(monkeypatch, bullets, on_warning=collected.append)

    assert bullets == BULLETS
    assert len(report.entries) == 1
    kind, line = report.entries[0]
    assert kind == rt_run.KIND_AIWRITING
    assert f"item '{ITEM}' was not swept" in line
    assert collected == [line]


def test_a_sweep_that_raises_outright_never_sinks_the_run(engine, monkeypatch):
    """`sweep_items` guards each item's model call itself, so anything reaching the pass
    is a defect. It is still caught: by the time this pass runs, every bullet is
    grounded, style-gated and inside its line budget, and losing that résumé over the
    stage that only polishes phrasing is the worse outcome. It is a warning, because
    unlike a refused rewrite the stage genuinely did not run."""
    def _boom(*_a, **_k):
        raise KeyError("atom the master does not hold")

    monkeypatch.setattr(sweep, "sweep_items", _boom)
    collected: list[str] = []
    bullets = dict(BULLETS)
    report = _drive(monkeypatch, bullets, on_warning=collected.append)

    assert bullets == BULLETS
    assert len(report.entries) == 1 and report.entries[0][0] == rt_run.KIND_AIWRITING
    assert "skipped" in report.entries[0][1]
    assert collected == [report.entries[0][1]]


# ── the re-trim backstop ─────────────────────────────────────────────────────
def test_the_retrim_after_the_sweep_changes_nothing(engine, monkeypatch):
    """`retrim=True` is a backstop, and it must stay a provable no-op.

    The sweep refuses any rewrite that renders past its bullet's line budget, so nothing
    reaching `_trim_to_caps` is ever over-length. A trim firing here would not be the
    backstop working: it would be evidence that the acceptance check has a hole, and it
    would silently turn a rewrite that should have been rejected into a mid-sentence
    bullet, which is the exact outcome the fit-or-revert design exists to prevent.
    """
    seen: list = []
    real_trim = rt_run._trim_to_caps

    def _recording_trim(sel, bullets):
        before = dict(bullets)
        real_trim(sel, bullets)
        seen.append((before, dict(bullets)))

    monkeypatch.setattr(rt_run, "_trim_to_caps", _recording_trim)
    monkeypatch.setattr(sweep, "call", Recorder(answer=_answer(
        b1=_REPAIRED_B1, b2=_REFUSED_B2, b3=BULLETS["b3"])))
    _drive(monkeypatch, dict(BULLETS))

    assert len(seen) == 1                    # the driver re-trimmed exactly once
    before, after = seen[0]
    assert before == after                   # ... and it cut nothing
    assert before["b1"] == _REPAIRED_B1      # ... on the bullet the sweep really moved
