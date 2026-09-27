"""TL-7 to TL-9 as the run meets them: the three Jev options, each off by default.

  TL-7 best of three   the rephrase writes three drafts of every bullet; the drafts
                       that pass the grounding gate and TL-4 go to Jev, which keeps
                       the one that shows the most of what the job asks for.
  TL-8 letter check    Jev reads each cover-letter sentence against the letter's
                       sources; a flagged sentence goes to the letter's repair call.
  TL-9 ATS meaning     Jev reads each ATS keyword against the résumé's text, for a
                       meaning-level coverage line beside the literal one.

Each is read through `config.best_of_n()`, `config.cover_letter_jev_check()` and
`config.ats_meaning()`, and runs only while Jev is on for the tailor. With an option
off, or Jev off, the run is today's: `test_tailor_jev.py` pins the rephrase prompt
with all three off, and with all three on while Jev is off.

Jev picks and flags; it writes no text. Every draft is the rephrase's own, and the
letter's repair call writes any new letter text.

Nothing here calls a model or Jev: every `call` is replaced, and every judge is a
fake.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from resume_tailor import assets, compose, config, jev_assist  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402

import test_tailor_faithfulness as faith  # noqa: E402 - sibling test module
import test_tailor_golden as golden  # noqa: E402
import test_tailor_jev as tailor_jev  # noqa: E402

# Fixtures re-exported by assignment (see test_sweep_layout_invariant.py).
engine = faith.engine
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

_H1, _H1_LED, _H2 = faith._H1, faith._H1_LED, faith._H2
# A second grounded draft of h1, with every token in its atom.
_H1_ALT = "Helped move the billing service to a queue-based design with the team."
_H2_KAFKA = "Wrote 40 integration tests for the billing service on Kafka."


@pytest.fixture()
def prompt_assets(monkeypatch):
    """The rephrase prompt's file-backed assets, pinned."""
    monkeypatch.setattr(assets, "example_text", lambda: "Exemplar voice, fixed.")
    monkeypatch.setattr(assets, "active_verbs",
                        lambda: {"Building": ["Wrote", "Built"], "Helping": ["Helped"]})


class Judge:
    """TL-4 as `faith.Flagger` answers it (a bullet opening with one of `prefixes`
    inflates), and every TL-7 choice picked as `pick` (the first draft when `pick`
    is not an option). `choice_down` makes every TL-7 request raise."""

    def __init__(self, *prefixes, pick="draft 2", choice_down=False):
        self.flagger = faith.Flagger(*prefixes)
        self.pick = pick
        self.choice_down = choice_down
        self.faith_requests = []
        self.choice_requests = []

    def judge(self, state, questions):
        if "bullets" in state:
            self.faith_requests.append(list(state["bullets"]))
            return self.flagger.judge(state, questions)
        self.choice_requests.append(questions)
        if self.choice_down:
            raise RuntimeError("the judge is down")
        out = {}
        for qid, q in questions.items():
            names = list(q["criteria"])
            pick = self.pick if self.pick in names else names[0]
            out[qid] = jev.Answer(kind="choice", choice=pick, confidence=0.8,
                                  probabilities={n: float(n == pick) for n in names})
        return out


def _best_of(monkeypatch, drafts, judge):
    monkeypatch.setattr(compose, "rephrase_drafts", lambda *a, **k: drafts)
    logs = []
    got = rt_run._resolve_best_of("Platform role.", "Engineer", faith._sel(), logs.append,
                                  briefs=None, judge=judge)
    return got, logs


# ── TL-7: the drafts prompt ───────────────────────────────────────────────────
def test_the_drafts_prompt_is_the_rephrase_prompt_with_the_drafts_rule(engine, prompt_assets,
                                                                       monkeypatch):
    rec = faith.Recorder({"bullets": []})
    monkeypatch.setattr(compose, "call", rec)
    compose.rephrase("Platform role.", "Engineer", faith._sel())
    compose.rephrase_drafts("Platform role.", "Engineer", faith._sel())
    today, drafts = rec.systems, rec.users
    assert today[1] == today[0] + compose.REPHRASE_DRAFTS_RULE
    assert drafts[1] == drafts[0] + compose.REPHRASE_DRAFTS_SHAPE
    assert compose.REPHRASE_DRAFTS_RULE not in today[0]
    assert compose.REPHRASE_DRAFTS_SHAPE not in drafts[0]


def test_the_drafts_rule_asks_for_three_drafts_of_the_same_atoms():
    rule = compose.REPHRASE_DRAFTS_RULE + compose.REPHRASE_DRAFTS_SHAPE
    assert compose.REPHRASE_DRAFTS == 3
    assert "three drafts" in rule and '"texts"' in rule
    assert compose.style_violations(rule) == [] and chr(0x2014) not in rule


def test_rephrase_drafts_reads_up_to_three_distinct_texts_per_bullet(engine, prompt_assets,
                                                                     monkeypatch):
    monkeypatch.setattr(compose, "call", faith.Recorder({"bullets": [
        {"gkey": "h1", "texts": [" a ", "b", "a", "", 7, "c", "d"]},
        {"gkey": "h2", "text": "x"},              # the single shape reads as one draft
        {"gkey": "zz", "texts": ["q"]},           # no such group
        "junk"]}))
    got = compose.rephrase_drafts("Platform role.", "Engineer", faith._sel())
    assert got == {"h1": ["a", "b", "c"], "h2": ["x"]}


# ── TL-7: the pick ────────────────────────────────────────────────────────────
def test_jev_picks_among_the_drafts_that_pass_the_gate_and_tl4(engine, monkeypatch):
    """h1's first draft inflates and h2's first is ungrounded, so neither goes to
    the pick: h1 has two drafts left, and Jev keeps its second; h2 has one, which
    needs no question."""
    judge = Judge("led", pick="draft 2")
    got, logs = _best_of(monkeypatch, {"h1": [_H1_LED, _H1, _H1_ALT],
                                       "h2": [_H2_KAFKA, _H2]}, judge)
    assert got == {"h1": _H1_ALT, "h2": _H2}
    # TL-4 read every grounded draft once, h2's Kafka draft never
    assert judge.faith_requests == [[_H1_LED, _H1, _H1_ALT, _H2]]
    (questions,), = [judge.choice_requests]
    assert questions == {"bullet_0": {
        "type": "choice", "instructions": jev_assist.BEST_DRAFT_QUESTION,
        "criteria": {"draft 1": _H1, "draft 2": _H1_ALT}}}
    assert logs == ["rephrased 2 bullet(s) from 5 draft(s); Jev picked among the "
                    "passing drafts of 1"]


def test_with_no_draft_passing_the_first_draft_goes_on(engine, monkeypatch):
    """The rephrase's first draft is what the run would have had with the option
    off; the prologue gate and TL-4 then treat it as they treat any bullet."""
    judge = Judge("led", "wrote")
    got, _logs = _best_of(monkeypatch, {"h1": [_H1_LED], "h2": [_H2_KAFKA, _H2]}, judge)
    assert got == {"h1": _H1_LED, "h2": _H2_KAFKA}
    assert judge.choice_requests == []


def test_a_pick_that_fails_keeps_the_first_passing_draft(engine, monkeypatch):
    judge = Judge("led", choice_down=True)
    got, _logs = _best_of(monkeypatch, {"h1": [_H1_LED, _H1, _H1_ALT], "h2": [_H2]}, judge)
    assert got == {"h1": _H1, "h2": _H2}
    assert "fell back to the LLM path (RuntimeError)" in jev_assist.usage_line(
        jev_assist.STEP_BEST_OF)


def test_a_tl4_check_that_cannot_run_leaves_the_gate_alone(engine, monkeypatch):
    """TL-4 down: every grounded draft passes, as every bullet does when the gate
    stands alone, and the pick still runs."""
    class FaithDown(Judge):
        def judge(self, state, questions):
            if "bullets" in state:
                raise RuntimeError("the judge is down")
            return super().judge(state, questions)

    judge = FaithDown(pick="draft 3")
    got, _logs = _best_of(monkeypatch, {"h1": [_H1_LED, _H1, _H1_ALT], "h2": [_H2]}, judge)
    assert got == {"h1": _H1_ALT, "h2": _H2}


def test_drafts_with_no_bullet_fall_back_to_the_rephrase(engine, monkeypatch):
    """A drafts answer that carries nothing reads as a failed call: the run makes
    today's rephrase call, so the option can never cost the run its bullets."""
    monkeypatch.setattr(compose, "rephrase", lambda *a, **k: {"h1": _H1, "h2": _H2})
    got, _logs = _best_of(monkeypatch, {}, Judge())
    assert got == {"h1": _H1, "h2": _H2}


def test_the_pick_is_always_one_of_the_rephrases_drafts(engine, monkeypatch):
    judge = Judge(pick="draft 9")
    got, _logs = _best_of(monkeypatch, {"h1": [_H1, _H1_ALT], "h2": [_H2]}, judge)
    assert got["h1"] in (_H1, _H1_ALT)


# ── TL-7 in the whole run ─────────────────────────────────────────────────────
def _drafts_asked(calls):
    return [c for c in calls if c["stage"] == "rephrase"
            and compose.REPHRASE_DRAFTS_SHAPE in c["user"]]


@pytest.mark.parametrize("option", ["1", "0"], ids=["option on", "option off"])
def test_the_run_asks_for_drafts_only_with_the_option_and_jev_on(
        pinned_engine, stub_template_head, tmp_path, monkeypatch, option):
    monkeypatch.setenv("RESUME_TAILOR_BEST_OF_N", option)
    stages, calls = [], []
    golden._install_stub(monkeypatch, tailor_jev._recording(stages, calls))
    tailor_jev._jev_on(monkeypatch, jev.FakeJev())
    tailor_jev._run_tailor(monkeypatch, tmp_path)
    report = tailor_jev._report(tmp_path)
    assert stages.count("rephrase") == 1
    assert len(_drafts_asked(calls)) == (1 if option == "1" else 0)
    assert ("jev best of three:" in report) == (option == "1")


def test_with_jev_off_the_option_asks_for_no_drafts(pinned_engine, stub_template_head,
                                                    tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_BEST_OF_N", "1")
    stages, calls = [], []
    golden._install_stub(monkeypatch, tailor_jev._recording(stages, calls))
    tailor_jev._jev_off(monkeypatch)
    tailor_jev._run_tailor(monkeypatch, tmp_path)
    assert _drafts_asked(calls) == [] and stages.count("rephrase") == 1
    assert "jev best of three" not in tailor_jev._report(tmp_path)


def test_the_option_getters_default_off(monkeypatch):
    monkeypatch.setattr(config, "_config_json", lambda: {})
    for env in tailor_jev.JEV_OPTION_ENVS:
        monkeypatch.delenv(env, raising=False)
    assert (config.best_of_n(), config.cover_letter_jev_check(), config.ats_meaning()) == (
        False, False, False)
