"""The screening set (cycle 18, TS-1, TS-2): every shipped screening question
run through the runner's own mapping and option pick (`apply_screening`, the
module the "Test my answers" button runs) for two synthetic profiles, under
the fake, the noisy and the real judge.

Rule per case (one question, one profile): the run picks the expected option
or nothing. Any other option is a wrong pick, and so is any answer where the
expectation is null. A floor on the share of non-null expectations picked
correctly keeps a run that picks nothing from passing.

- fake (`jev.FakeJev`): its word overlap cannot read every question, so its
  wrong picks are pinned in FAKE_MISREADS with their causes. The run makes
  exactly those; a new one fails, and so does a pinned one that goes away
  (move the pin and the floor together).
- noisy (`jev.NoisyJev` over the fake, seeds 1 to 5): it weakens and drops
  answers and never swaps a field's pick, so its wrong picks stay within the
  fake's.
- real (the `jev_judge` fixture, `AUTO_APPLY_TEST_JEV` record or replay; the
  runner target of `scripts/jev_record.ps1` records it): no wrong pick outside
  REAL_MISREADS (empty). The rule raises `pytest.fail`, which the harness
  never turns into a recorded divergence (an xfail). REAL_PICK_FLOOR is None
  (report only) until the orchestrator sets it from the recording; the rate
  prints either way. The fixture pins the catalog's `today` to the recording
  day, as it does for the runner tests. In fake mode the test skips: its judge
  would be the fake, which the fake test holds already.

The questions ship with the app (`local/screening_questions.json`, read
through `apply_screening.load_questions`); the profiles are test data
(`tests/fixtures/screening/profiles.json`), two typed v2 stores with every
entry confirmed.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_screening  # noqa: E402
import jev  # noqa: E402
import jev_harness  # noqa: E402
from resume_tailor import apply_answers, apply_config  # noqa: E402

pytest_plugins = ["conftest_jev"]

PROFILES_PATH = REPO / "tests" / "fixtures" / "screening" / "profiles.json"
PROFILES: dict[str, dict] = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
QUESTIONS = apply_screening.load_questions()
SEEDS = (1, 2, 3, 4, 5)

# Share of non-null expectations picked correctly, at the rate the final run
# measured (66 questions, 103 non-null expectations over both profiles),
# rounded down to two places. Cycle 18 SP6c lowered them from 0.61 and
# {0.60, 0.60, 0.60, 0.61, 0.59} (63; 62 62 62 63 61): a yes / no or years fact
# now gives no value to a question it does not answer
# (`apply_facts.asks_own_question`), which takes away nine picks the fake made
# right by chance through another question's fact and adds four through the
# derived facts.
FAKE_PICK_FLOOR = 0.56                                        # 58 of 103
NOISY_PICK_FLOOR = {1: 0.55, 2: 0.55, 3: 0.55, 4: 0.56, 5: 0.54}  # 57 57 57 58 56
# The real judge's floor: None reports the rate and checks nothing. The
# orchestrator sets it from the recording (TS-3).
REAL_PICK_FLOOR: float | None = None
# (question id, profile) cases where a wrong pick by the real judge is
# accepted, each with its cause. Empty: every wrong pick fails.
REAL_MISREADS: frozenset[tuple[str, str]] = frozenset()

# (question id, profile) cases the fake picks wrong. The fake answers by word
# overlap at full confidence. Both profiles give it the same words for a
# worded option (every stored yes or no fact as "DESCRIPTION: Yes/No"), so it
# picks by option order. A mapping to another question's fact no longer gives
# a wrong pick: the own-question gate (cycle 18, SP6c) leaves that field
# without a value.
FAKE_MISREADS: frozenset[tuple[str, str]] = frozenset({
    ("auth_contoso", "sponsor"),
    ("auth_with_without", "citizen"),
    ("auth_with_without_radio", "sponsor"),
    ("spon_qualified", "citizen"),
    ("spon_now_or_future", "citizen"),
    ("spon_now_or_future", "sponsor"),
    ("reloc_replica", "sponsor"),
})


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No test here reads or writes the real stores."""
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")


@dataclass
class Tally:
    judge: str
    right: int = 0
    expected: int = 0                        # non-null expectations
    wrong: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.right / self.expected if self.expected else 0.0

    def line(self) -> str:
        return (f"screening {self.judge}: {self.right}/{self.expected} expected picks "
                f"({self.rate:.1%}), {len(self.wrong)} wrong")


def tally(name: str, judge) -> Tally:
    """Every question for every profile through `apply_screening.run_screening`."""
    t = Tally(name)
    by_id = {q["id"]: q for q in QUESTIONS}
    for profile, store in PROFILES.items():
        for row in apply_screening.run_screening(store["answers"], judge):
            want = by_id[row.qid]["expect"][profile]
            t.expected += want is not None
            if row.answer is None:
                continue
            if row.answer == want:
                t.right += 1
            else:
                t.wrong[(row.qid, profile)] = (row.answer, want)
    return t


def hold(t: Tally, allowed: frozenset, floor: float | None, *, exact: bool = False) -> None:
    """The rule, raised with `pytest.fail` so a record or replay run keeps it a failure."""
    new = sorted(k for k in t.wrong if k not in allowed)
    if new:
        pytest.fail(f"{t.line()}; wrong picks outside the pinned set:\n" + "\n".join(
            f"  {qid} ({profile}): picked {t.wrong[(qid, profile)][0]!r}, "
            f"expected {t.wrong[(qid, profile)][1]!r}" for qid, profile in new))
    gone = sorted(allowed - set(t.wrong)) if exact else []
    if gone:
        pytest.fail(f"{t.line()}; pinned wrong picks that no longer happen (unpin them "
                    f"and raise the floor): {gone}")
    if floor is not None and t.rate < floor:
        pytest.fail(f"{t.line()}; below the floor {floor:.0%}")


def test_fake_judge_picks_the_expected_option_or_nothing():
    t = tally("fake", jev.FakeJev())
    print(t.line())
    hold(t, FAKE_MISREADS, FAKE_PICK_FLOOR, exact=True)


@pytest.mark.parametrize("seed", SEEDS)
def test_noisy_judge_picks_wrong_only_where_the_fake_does(seed):
    t = tally(f"noisy-{seed}", jev.NoisyJev(jev.FakeJev(), seed))
    print(t.line())
    hold(t, FAKE_MISREADS, NOISY_PICK_FLOOR[seed])


def test_real_judge_picks_the_expected_option_or_nothing(jev_judge, capsys):
    if jev_harness.mode_from(os.environ) == "fake":
        pytest.skip(f"the real judge runs with {jev_harness.MODE_ENV}=record or replay; "
                    f"in fake mode its judge is the fake")
    t = tally("real", jev_judge())
    with capsys.disabled():
        print(f"\n{t.line()}")
    if jev_harness.dry_from(os.environ):
        # the dry run answers with the fake at each request's live size
        hold(t, FAKE_MISREADS, FAKE_PICK_FLOOR)
        return
    hold(t, REAL_MISREADS, REAL_PICK_FLOOR)


def test_a_wrong_pick_fails_past_the_harness_divergence_path():
    """Record and replay mode turn a failed AssertionError into an xfail
    (`conftest_jev`); the rule's failure stays a failure there."""
    t = Tally("stub", right=1, expected=2, wrong={("auth_radio", "citizen"): ("No", "Yes")})
    with pytest.raises(pytest.fail.Exception) as wrong:
        hold(t, REAL_MISREADS, None)
    assert not isinstance(wrong.value, AssertionError)
    assert "auth_radio (citizen): picked 'No', expected 'Yes'" in str(wrong.value)
    with pytest.raises(pytest.fail.Exception, match="below the floor"):
        hold(Tally("stub", right=1, expected=2), REAL_MISREADS, 0.75)


def test_run_screening_gives_one_row_per_question_in_order():
    rows = apply_screening.run_screening(PROFILES["citizen"]["answers"], jev.FakeJev())
    assert [r.qid for r in rows] == [q["id"] for q in QUESTIONS]
    assert [r.question for r in rows] == [q["label"] for q in QUESTIONS]
    for r, q in zip(rows, QUESTIONS):
        assert r.answer is None or r.answer in q["options"], (r, q["options"])
    assert any(r.answer is None for r in rows) and any(r.answer for r in rows)
    with pytest.raises(AttributeError):
        rows[0].answer = "Yes"               # frozen: a caller cannot edit a row


def test_load_questions_reads_the_shipped_file_and_refuses_one_without_questions(tmp_path):
    shipped = json.loads(apply_screening.QUESTIONS_PATH.read_text(encoding="utf-8"))
    assert apply_screening.QUESTIONS_PATH == REPO / "local" / "screening_questions.json"
    assert apply_screening.load_questions() == shipped["questions"]
    bad = tmp_path / "questions.json"
    bad.write_text('{"version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="no \"questions\" list"):
        apply_screening.load_questions(bad)


def test_every_question_is_well_formed():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids)), "question ids repeat"
    facts = set(apply_screening.catalog_for(PROFILES["citizen"]["answers"]).facts)
    for q in QUESTIONS:
        assert set(q) == {"id", "label", "help", "widget", "options", "required",
                          "expected_fact", "expect"}, q["id"]
        assert q["widget"] in apply_screening.WIDGETS, q["id"]
        assert q["label"].strip() and q["options"], q["id"]
        assert len(q["options"]) == len(set(q["options"])), q["id"]
        assert q["required"] is True, q["id"]   # None always means the run stops here
        assert q["expected_fact"] is None or q["expected_fact"] in facts, q["id"]
        assert set(q["expect"]) == set(PROFILES), q["id"]
        for profile, want in q["expect"].items():
            assert want is None or want in q["options"], (q["id"], profile, want)
        if q["widget"] == "toggle":
            assert q["options"] == ["Yes", "No"], q["id"]
        # each question becomes the one field the extractor would read
        f = apply_screening.field_for(q)
        assert (f.label, f.options, f.required) == (q["label"], q["options"], True)


def test_the_set_covers_the_topics_the_spec_names():
    assert 55 <= len(QUESTIONS) <= 70
    assert {q["widget"] for q in QUESTIONS} == set(apply_screening.WIDGETS)
    prefixes = {q["id"].split("_")[0] for q in QUESTIONS}
    assert {"auth", "spon", "reloc", "onsite", "years", "gender", "race", "veteran",
            "disability", "hear", "none"} <= prefixes
    words = " ".join(" ".join([q["label"], *q["options"]]) for q in QUESTIONS).lower()
    for topic in ("OPT", "CPT", "H-1B", "in the future", "relocate", "hybrid", "on-site",
                  "days a week", "hear about"):
        assert topic.lower() in words, topic
    pairs = [q for q in QUESTIONS
             if {"Yes, with sponsorship", "Yes, without sponsorship"} <= set(q["options"])]
    assert len(pairs) >= 2
    contoso = [q for q in QUESTIONS if q["expected_fact"] == "willing_to_relocate"
               and any("relocate" in o for o in q["options"])
               and any("office" in o for o in q["options"])]
    assert contoso, "no relocation question whose options also answer on-site work"
    for profile in PROFILES:
        wants = [q["expect"][profile] for q in QUESTIONS]
        assert any(w is None for w in wants) and any(w is not None for w in wants)


def test_the_profiles_are_confirmed_v2_stores_with_their_stated_answers():
    stated = {
        "citizen": {"work_authorized": "Yes", "requires_sponsorship": "No",
                    "willing_to_relocate": "Yes", "onsite_ok": "Yes", "years_experience": "1"},
        "sponsor": {"work_authorized": "Yes", "requires_sponsorship": "Yes",
                    "willing_to_relocate": "No", "onsite_ok": "Yes", "years_experience": "3"},
    }
    assert set(PROFILES) == set(stated)
    for name, store in PROFILES.items():
        answers = store["answers"]
        assert store["version"] == apply_answers.VERSION
        assert apply_answers.validate(answers) == [], name
        assert all(e["confirmed"] is True for e in answers), name
        assert set(apply_answers.BUILTINS) <= {e["id"] for e in answers}, name
        got = {e["id"]: e["answer"] for e in answers}
        assert {k: got[k] for k in stated[name]} == stated[name]
