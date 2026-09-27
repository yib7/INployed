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
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jev  # noqa: E402
from resume_tailor import assets, compose, config, coverletter, jev_assist, verify  # noqa: E402
from resume_tailor import run as rt_run  # noqa: E402
from resume_tailor.llm import LLMError  # noqa: E402

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


# ── TL-8: the cover letter check ──────────────────────────────────────────────
_BULLETS = {"a1": "Wrote SQL reports on warehouse sales data."}
_BACKGROUND = "- Acme Data, Analyst Intern\n    - wrote SQL reports on warehouse sales data"
_SEED = "I want work where the data is the product."
_JD = "Data Analyst at Initech. Lead our analytics team and own the warehouse."
_RESEARCH = "Initech makes TPS report software."
_TRUE = "At Acme Data I wrote SQL reports on warehouse sales data."
_CLAIM = "I led the analytics team through a warehouse migration."
_BODY = f"{_TRUE} {_CLAIM}\n\nI would like to bring that work to Initech."
_REPAIRED = f"{_TRUE}\n\nI would like to bring that work to Initech."


class LetterCalls:
    """`compose.call` for the letter: the draft, then the repair. Keeps each call's
    role, system and user prompt."""

    def __init__(self):
        self.calls = []

    def __call__(self, system, user, tier, **kw):
        role = "repair" if "You repair a cover-letter body" in system else "draft"
        self.calls.append((role, system, user))
        return _REPAIRED if role == "repair" else _BODY

    def roles(self):
        return [role for role, _s, _u in self.calls]

    def repair_user(self):
        (user,) = [u for role, _s, u in self.calls if role == "repair"]
        return user


class ClaimReader:
    """A TL-8 judge: a sentence holding one of `words` claims more than its sources
    (0.9); every other sentence passes. Keeps each request's state."""

    def __init__(self, *words, down=False):
        self.words = tuple(w.lower() for w in words)
        self.down = down
        self.states = []

    def judge(self, state, questions):
        self.states.append(state)
        if self.down:
            raise RuntimeError("the judge is down")
        return {f"claims_{i}": jev.Answer(
                    kind="noul", noul=0.9 if any(w in s.lower() for w in self.words) else 0.1)
                for i, s in enumerate(state["sentences"])}


@pytest.fixture()
def letter(monkeypatch):
    """generate_body with its two middle passes and its master reads pinned, and the
    deterministic gate answering from `unseen` ({body: tokens})."""
    calls = LetterCalls()
    unseen = {}
    monkeypatch.setattr(compose, "call", calls)
    monkeypatch.setattr(coverletter, "refine_body", lambda jt, c, body, *a, **k: body)
    monkeypatch.setattr(coverletter, "enforce_body_style", lambda jt, c, body, *a, **k: body)
    monkeypatch.setattr(verify, "letter_unseen", lambda body, allowed: list(unseen.get(body, [])))
    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"name": "Sam Rivera", "location": "Austin"}})
    monkeypatch.setattr(coverletter, "_education_context", lambda: "BS Statistics: graduated")
    jev_assist.reset_usage()

    def generate(**kw):
        return coverletter.generate_body(_JD, "Data Analyst", "Initech", dict(_BULLETS),
                                         research=_RESEARCH, background=_BACKGROUND,
                                         seed=_SEED, **kw)
    return types.SimpleNamespace(calls=calls, unseen=unseen, generate=generate)


def test_a_flagged_sentence_goes_to_the_repair_call(letter):
    body = letter.generate(judge=ClaimReader("led"))
    assert body == _REPAIRED
    assert letter.calls.roles() == ["draft", "repair"]
    user = letter.calls.repair_user()
    assert coverletter.LETTER_CLAIMS_NOTE in user and f"- {_CLAIM}" in user
    # the gate found nothing, so the repair names no unsupported item
    assert "UNSUPPORTED ITEMS TO REMOVE" not in user


def test_the_check_reads_every_sentence_against_the_sources_never_the_job(letter):
    judge = ClaimReader()
    letter.generate(judge=judge)
    (state,) = judge.states
    assert state["sentences"] == [_TRUE, _CLAIM, "I would like to bring that work to Initech."]
    assert state["sources"] == {
        "resume bullets": list(_BULLETS.values()), "background": _BACKGROUND,
        "own words": _SEED, "basics": "Sam Rivera, Austin. Education: BS Statistics: graduated"}
    assert _JD not in json.dumps(state) and _RESEARCH not in json.dumps(state)


def test_with_nothing_flagged_the_letter_is_todays(letter):
    assert letter.generate(judge=ClaimReader()) == _BODY
    assert letter.calls.roles() == ["draft"]


@pytest.mark.parametrize("judge", [None, ClaimReader("led", down=True)],
                         ids=["no judge", "failing judge"])
def test_without_a_working_check_the_path_is_todays(letter, judge):
    kw = {} if judge is None else {"judge": judge}
    assert letter.generate(**kw) == _BODY
    assert letter.calls.roles() == ["draft"]


def test_the_gate_and_the_check_share_one_repair_and_the_gate_decides(letter):
    """Both find something: one repair names both; the repaired body still holding a
    gate finding fails the letter, as it does today."""
    letter.unseen[_BODY] = ["Kubernetes"]
    letter.unseen[_REPAIRED] = ["Kubernetes"]
    with pytest.raises(LLMError):
        letter.generate(judge=ClaimReader("led"))
    user = letter.calls.repair_user()
    assert "UNSUPPORTED ITEMS TO REMOVE" in user and "Kubernetes" in user
    assert coverletter.LETTER_CLAIMS_NOTE in user


def test_a_repair_with_no_claim_is_todays_prompt(letter):
    """The gate alone found something: the repair prompt is byte for byte what it
    was before TL-8, check on or off."""
    letter.unseen[_BODY] = ["Kubernetes"]
    letter.generate()
    today = letter.calls.repair_user()
    letter.calls.calls.clear()
    letter.generate(judge=ClaimReader())
    assert letter.calls.repair_user() == today
    assert coverletter.LETTER_CLAIMS_NOTE not in today


def test_the_claims_note_is_free_of_the_banned_phrasing():
    note = coverletter.LETTER_CLAIMS_NOTE
    assert compose.style_violations(note) == [] and chr(0x2014) not in note


# ── TL-8 in the run ───────────────────────────────────────────────────────────
def _letter_run(monkeypatch, tmp_path, option):
    """The golden run with a cover letter: the body call is captured and the render
    fails (an advisory), so the run reaches the letter and goes on."""
    monkeypatch.setenv("RESUME_TAILOR_COVER_LETTER_JEV_CHECK", option)
    got = {}

    def fake_body(jd, job_title, company, bullets, research="", tone="professional",
                  background="", seed="", **kw):
        got.update(kw)
        return "body"

    monkeypatch.setattr(coverletter, "generate_body", fake_body)
    monkeypatch.setattr(coverletter, "render_cover_letter",
                        lambda *a, **k: (types.SimpleNamespace(ok=False, pdf_path=None,
                                                               error="stub"), ""))
    tailor_jev._run_tailor(monkeypatch, tmp_path, cover_letter=True)
    return got


@pytest.mark.parametrize("option", ["1", "0"], ids=["option on", "option off"])
def test_the_run_hands_the_letter_its_judge_only_with_the_option(
        pinned_engine, stub_template_head, tmp_path, monkeypatch, option):
    golden._install_stub(monkeypatch, tailor_jev._recording([], []))
    tailor_jev._jev_on(monkeypatch, jev.FakeJev())
    got = _letter_run(monkeypatch, tmp_path, option)
    assert ("judge" in got) == (option == "1")
    assert ("jev letter check:" in tailor_jev._report(tmp_path)) == (option == "1")


def test_with_jev_off_the_letter_gets_no_judge(pinned_engine, stub_template_head,
                                               tmp_path, monkeypatch):
    golden._install_stub(monkeypatch, tailor_jev._recording([], []))
    tailor_jev._jev_off(monkeypatch)
    assert "judge" not in _letter_run(monkeypatch, tmp_path, "1")


@pytest.mark.parametrize("option", ["1", "0"], ids=["option on", "option off"])
def test_the_standalone_letter_builds_a_judge_only_with_the_option(tmp_path, monkeypatch,
                                                                  option):
    """The Generate cover letter button: with the check on it builds the tailor's
    judge and hands it over; with it off it never asks for one."""
    import test_coverletter_inputs as cl_inputs
    monkeypatch.setenv("RESUME_TAILOR_COVER_LETTER_JEV_CHECK", option)
    areas = tailor_jev._jev_on(monkeypatch, jev.FakeJev())
    monkeypatch.setattr(rt_run.assets, "load_master", lambda: cl_inputs.MASTER)
    monkeypatch.setattr(rt_run, "pdflatex_available", lambda: True)
    monkeypatch.setattr(rt_run.research, "company_blurb", lambda *a, **k: "")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "apply.md").write_text(
        "# Apply sheet\n\n## Work experience\n\n**X** Intern\n\n- Built it.\n\n"
        "<!-- inployed-apply-meta: {\"job_posting_id\": \"1\"} -->\n", encoding="utf-8")
    got = {}

    def fake_body(jd, job_title, company, bullets, research="", tone="professional",
                  background="", seed="", **kw):
        got.update(kw)
        return "body"

    monkeypatch.setattr(rt_run.coverletter, "generate_body", fake_body)
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(rt_run.coverletter, "render_cover_letter", cl_inputs._fake_render(pdf))
    monkeypatch.setattr(rt_run.coverletter, "cover_letter_text", lambda b, c: "txt")
    logs = []
    job = {"company_name": "BigCo", "job_title": "Engineer", "job_description": "x" * 200}
    rt_run.generate_cover_letter(job, out_dir, on_status=logs.append)
    assert ("judge" in got) == (option == "1")
    assert areas == (["tailor"] if option == "1" else [])
    assert any(line.startswith("jev letter check:") for line in logs) == (option == "1")
