"""The layout invariant of the item-level AI-writing sweep, proved over the golden fixture.

The worry this feature had to answer was never whether a model can write a cleaner
bullet. It was whether cleaning the bullets wrecks the page. A résumé is one page by
construction: `run._trim_to_caps` gives every bullet a printed-line budget, and
`compile.enforce_one_page` drops whole bullets when the document still overflows. A
stage that hands back longer text than it was given spends that budget, and the cost is
paid at the bottom of the page, in a bullet nobody chose to lose.

`sweep.py` answers it with one acceptance condition: a rewrite is committed only when
`measure.line_count(new) <= target_lines` for that bullet, the same budget the trim
enforces. So this file states the consequence as a property rather than as a case:

* **every** bullet the sweep leaves behind renders within its own budget, and
* **every item's** total printed line count is at or below what it was before the pass,

for a whole realistic résumé rather than one hand-picked bullet, and while the model is
actively hostile. `tests/test_item_sweep.py` proves each acceptance condition rejects
what it is written to reject, one condition at a time; this file proves the page cannot
grow no matter what comes back.

── how the résumé under test is built ────────────────────────────────────────────

From `tests/test_tailor_golden.py`, imported rather than rebuilt. That module already
pins a synthetic candidate, a fixed JD, every config toggle the pipeline reads, and a
stubbed transport, and its `_run_bullet_pipeline` is a transcript of `run.tailor()`'s
bullet middle. Running it with the sweep toggled OFF yields exactly the state the style
gate leaves behind: 8 bullets across 4 items, each already trimmed to its budget. A
second copy of that fixture here would drift from it, and the drift would be silent.

`test_the_pre_sweep_resume_is_the_golden_one` pins that reading, because every assertion
below is measured against those bullets.

── the adversary ─────────────────────────────────────────────────────────────────

One shape per way a rewrite can be hostile to the page, applied to every bullet of every
item, in both the item call and the bounded re-ask:

* `_much_longer`  — a rewrite that runs far past the budget.
* `_one_char_over`— the boundary case: the longest text that still fits, plus one
                    character. A budget check with an off-by-one is invisible to
                    `_much_longer` and fails here.
* `_shorter`      — a rewrite that genuinely fits, so the file is not passing merely
                    because everything is refused. `test_a_fitting_rewrite_is_committed`
                    pins that this one really does move the bullets.
* `_empty`        — the model answering with nothing.
* `_unchanged`    — the honest answer for an item with nothing wrong in it.

The shapes are built to fail on LENGTH and nothing else: each one keeps the original
text as its prefix (so no fact and no opening verb is lost) and pads with plain
lowercase filler that trips no ban in `compose._STYLE_BANS` or
`aiwriting.RESUME_EXTRA_BANS`. A rewrite refused for the wrong reason would still leave
the invariant standing while testing none of it, so
`test_the_hostile_shapes_are_hostile_about_length_only` checks that directly.

**Nothing here calls a model.** `sweep.call` is replaced in every test that reaches the
sweep, and the golden fixture's own stub raises on any prompt it does not recognise.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import (aiwriting, compose, config, itemcheck, measure,  # noqa: E402
                           sweep, verify)
from resume_tailor import run as rt_run  # noqa: E402

import test_tailor_golden as golden  # noqa: E402 - sibling test module, no pkg import

# The golden module's two fixtures, re-exported by assignment rather than imported by
# name. pytest collects a fixture found as a module attribute either way; ruff reads
# `from ... import pinned_engine` followed by a test taking `pinned_engine` as an
# argument as a redefinition (F811), and 14 per-test `noqa` comments to say otherwise
# is worse than one line saying it here.
pinned_engine = golden.pinned_engine
stub_template_head = golden.stub_template_head

# Plain, dull, lowercase, and long enough that one copy pushes any bullet in the fixture
# past a 3-line budget. Every word is checked against both ban lists by
# `test_the_filler_is_clean`, because filler that trips a ban would turn a length
# rejection into a style rejection and quietly stop testing the length check.
_FILLER = (" and kept the same weekly cadence for the rest of the term with notes "
           "written down after each session and shared with the group that asked for "
           "them and with the two people who joined later on")


# ── the adversarial shapes ───────────────────────────────────────────────────────
def _longest_fitting(text: str, target_lines: int) -> int:
    """Length of the longest prefix of `text` that renders within `target_lines`.

    Deliberately a second implementation of `sweep._fitting_prefix_len` rather than a
    call to it: the shapes below are the ruler this file measures the sweep with, so
    they must not be built from the code under test.
    """
    if measure.line_count(text) <= target_lines:
        return len(text)
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure.line_count(text[:mid]) <= target_lines:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _much_longer(text: str, target_lines: int) -> str:
    """Far past the budget: the original plus filler, twice over."""
    return text.rstrip(".") + _FILLER + _FILLER


def _one_char_over(text: str, target_lines: int) -> str:
    """The longest text that fits, plus exactly one character.

    Padding first and cutting back is what makes this exact: the result begins with the
    whole original bullet (so nothing is dropped) and is one character too long to print.
    """
    padded = text.rstrip(".") + _FILLER
    return padded[:_longest_fitting(padded, target_lines) + 1]


def _shorter(text: str, target_lines: int) -> str:
    """A rewrite that fits: the original with its last word dropped.

    The dropped word is lowercase and digit-free in every bullet of the fixture
    (`test_the_shorter_shape_drops_nothing_distinctive` pins that), so the rewrite keeps
    every fact `verify.unseen_tokens` traces and is committed rather than refused.
    """
    words = text.rstrip(".").split()
    return " ".join(words[:-1]) + "."


def _empty(text: str, target_lines: int) -> str:
    return ""


def _unchanged(text: str, target_lines: int) -> str:
    return text


_SHAPES = {
    "much_longer": _much_longer,
    "one_char_over": _one_char_over,
    "shorter": _shorter,
    "empty": _empty,
    "unchanged": _unchanged,
}


class Adversary:
    """A `sweep.call` replacement that answers with one shape per round.

    `first` answers the item call, `second` the bounded re-ask. Both read the bullets
    back out of the prompt body the sweep actually built, so an item the sweep never
    sent cannot be answered, and the two rounds are distinguished by the re-ask's own
    system prompt.
    """

    def __init__(self, first, second=None):
        self.first, self.second = first, second or first
        self.rounds: list = []

    def __call__(self, system, user, tier, **kw):
        body = golden._item_body(user)
        reask = "came back too long" in system
        self.rounds.append(("reask" if reask else "sweep", body["item"]))
        shape = self.second if reask else self.first
        return {"bullets": [
            # The re-ask names each bullet's own ORIGINAL, which is what a model
            # shortening its answer has to work from.
            {"gkey": b["gkey"],
             "text": shape(b.get("original") or b["text"], b["max_lines"])}
            for b in body["bullets"]]}


# ── driving the real pass over the real fixture ──────────────────────────────────
def _pre_sweep(monkeypatch):
    """The résumé exactly as the deterministic style gate leaves it: `(sel, bullets)`.

    The sweep is turned off through `config.json`, which is the real toggle, so the
    golden transcript runs its whole bullet middle and stops one stage short. The toggle
    is restored before returning, because the `Pass` reads it again through the driver.
    """
    monkeypatch.setattr(config, "_config_json",
                        lambda: dict(golden._CONFIG_JSON, resume_aiwriting_sweep=False))
    sel, bullets, _skill_lines, _tex = golden._run_bullet_pipeline()
    monkeypatch.setattr(config, "_config_json", lambda: dict(golden._CONFIG_JSON))
    return sel, bullets


def _run_sweep(monkeypatch, sel, bullets, adversary):
    """Run the REAL sweep `Pass` through the REAL driver, and record every trim.

    Going through `_run_bullet_passes` rather than calling `sweep.sweep_items` directly
    is the point of this file: the retrim and the grounding gate are part of what the
    page finally gets, and the retrim is the thing `test_the_retrim_is_a_no_op` watches.
    """
    trims: list = []
    real_trim = rt_run._trim_to_caps

    def _recording_trim(sel_, bullets_):
        before = dict(bullets_)
        real_trim(sel_, bullets_)
        trims.append((before, dict(bullets_)))

    monkeypatch.setattr(rt_run, "_trim_to_caps", _recording_trim)
    monkeypatch.setattr(sweep, "call", adversary)
    ctx = rt_run.PassCtx(jd=golden._JD, job_title=golden._JOB["job_title"], sel=sel,
                         bullets=bullets, verbatim={}, reserved=frozenset(),
                         log=lambda _m: None, report=rt_run.RunLog())
    sweep_pass = next(p for p in rt_run._BULLET_PASSES
                      if p.name == rt_run.AIWRITING_SWEEP_STAGE)
    rt_run._run_bullet_passes(ctx, passes=(sweep_pass,))
    return trims


def _line_counts(sel, bullets):
    """`{item: [(gkey, printed line count, that bullet's budget), ...]}`, item by item.

    Verbatim bullets are absent by construction: `_blocks_in_order` reads `group_map`,
    which excludes them, and neither the sweep nor the trim touches the user's own text.
    """
    targets = compose.bullet_line_targets(sel)
    out = {}
    for item, gkeys in compose._blocks_in_order(sel):
        out[item] = [(gk, measure.line_count(bullets[gk]),
                      targets.get(gk, config.PROJECT_BULLET_LINES))
                     for gk in gkeys if gk in bullets]
    return out


# ── the fixture has to be honest ─────────────────────────────────────────────────
def test_the_pre_sweep_resume_is_the_golden_one(pinned_engine, stub_template_head,
                                                monkeypatch):
    """With the sweep off, the pipeline produces the pinned golden bullets.

    Everything below measures against this state, so a change in the fixture that
    quietly emptied it would make every invariant hold vacuously.
    """
    _sel, bullets = _pre_sweep(monkeypatch)
    assert bullets == golden._GOLDEN_BULLETS
    assert "aiwriting_sweep" not in pinned_engine


def test_every_bullet_already_fits_before_the_sweep(pinned_engine, stub_template_head,
                                                    monkeypatch):
    """The starting text is already legal, so any overflow after the pass is the sweep's."""
    sel, bullets = _pre_sweep(monkeypatch)
    measured = _line_counts(sel, bullets)
    assert sorted(measured) == ["Globex Analytics", "Ledgerly", "Robotics Club",
                                "Trailhead"]
    assert sum(len(rows) for rows in measured.values()) == 8   # the 9th is verbatim
    for item, rows in measured.items():
        for gkey, lines, target in rows:
            assert lines <= target, f"{item}/{gkey} starts over its budget"


def test_the_filler_is_clean():
    """The padding trips no deterministic ban in either arm.

    Filler that did would turn every overflow rejection into a style rejection, and this
    whole file would then be testing condition 3 while claiming to test condition 2.
    """
    assert compose.style_violations(_FILLER) == []
    assert aiwriting.resume_violations(_FILLER) == []


def test_the_hostile_shapes_are_hostile_about_length_only(pinned_engine,
                                                          stub_template_head,
                                                          monkeypatch):
    """Each overflowing shape really does overflow, and fails on nothing else.

    A shape the sweep would refuse for a swapped verb, a dropped number or a banned
    phrase would leave the invariant standing while proving nothing about the line
    budget. So every one is checked against the other four acceptance conditions here,
    at the level of the conditions themselves.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    for _item, rows in _line_counts(sel, bullets).items():
        for gkey, _lines, target in rows:
            original = bullets[gkey]
            for name in ("much_longer", "one_char_over"):
                new = _SHAPES[name](original, target)
                assert measure.line_count(new) > target, (name, gkey)
                assert new.strip()
                assert (itemcheck.leading_verb(new)
                        == itemcheck.leading_verb(original)), (name, gkey)
                assert verify.unseen_tokens(original, new) == [], (name, gkey)
                fresh = [v for v in compose.style_violations(new)
                         + aiwriting.resume_violations(new)
                         if v not in compose.style_violations(original)
                         + aiwriting.resume_violations(original)]
                assert fresh == [], (name, gkey, fresh)


def test_the_one_char_over_shape_is_over_by_exactly_one_character(pinned_engine,
                                                                  stub_template_head,
                                                                  monkeypatch):
    """The boundary shape overflows, and dropping its last character makes it fit.

    That is what makes it a boundary: an acceptance check reading `<` where it means
    `<=`, or measuring the wrong budget, is caught here and nowhere else.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    for _item, rows in _line_counts(sel, bullets).items():
        for gkey, _lines, target in rows:
            over = _one_char_over(bullets[gkey], target)
            assert measure.line_count(over) == target + 1
            assert measure.line_count(over[:-1]) == target


def test_the_shorter_shape_drops_nothing_distinctive(pinned_engine, stub_template_head,
                                                     monkeypatch):
    """The fitting shape keeps every number and proper name, so the acceptance check has
    no reason of its own to refuse it and the commit path really is exercised."""
    sel, bullets = _pre_sweep(monkeypatch)
    for _item, rows in _line_counts(sel, bullets).items():
        for gkey, _lines, target in rows:
            original = bullets[gkey]
            new = _shorter(original, target)
            assert new != original
            assert measure.line_count(new) <= target
            assert verify.unseen_tokens(original, new) == [], gkey


# ── the invariant ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("first", sorted(_SHAPES))
@pytest.mark.parametrize("second", sorted(_SHAPES))
def test_the_printed_layout_never_grows(pinned_engine, stub_template_head, monkeypatch,
                                        first, second):
    """Every pairing of an item answer and a re-ask answer, over the whole fixture.

    Two properties, and they are different claims. The first is per bullet: nothing on
    the page renders past the budget `_trim_to_caps` set for it. The second is per item:
    the item's total printed line count is at or below what it was, which is the claim
    `compile.enforce_one_page` depends on, since a page grows by lines and not by
    bullets.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    before = _line_counts(sel, bullets)
    _run_sweep(monkeypatch, sel, bullets, Adversary(_SHAPES[first], _SHAPES[second]))
    after = _line_counts(sel, bullets)

    assert sorted(after) == sorted(before)              # no item lost its bullets
    for item, rows in after.items():
        for gkey, lines, target in rows:
            assert lines <= target, (
                f"{item}/{gkey} renders on {lines} lines against a budget of {target} "
                f"after a {first}/{second} sweep")
        was = sum(lines for _gk, lines, _t in before[item])
        now = sum(lines for _gk, lines, _t in rows)
        assert now <= was, (
            f"item '{item}' grew from {was} to {now} printed lines after a "
            f"{first}/{second} sweep")


@pytest.mark.parametrize("first", sorted(_SHAPES))
@pytest.mark.parametrize("second", sorted(_SHAPES))
def test_the_retrim_is_a_no_op(pinned_engine, stub_template_head, monkeypatch,
                               first, second):
    """`retrim=True` on the sweep pass is a backstop, and it must never fire.

    If the acceptance check is right, no bullet reaches `_trim_to_caps` over-length, so
    the trim has nothing to cut. A trim that changes text here is the report that the
    acceptance check has a hole: a silent trim converts a rewrite that should have been
    rejected into a mid-sentence bullet, which is the exact outcome the fit-or-revert
    design exists to prevent. Whatever makes this fail is a defect in `sweep._accept`,
    and this test is only the messenger.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    trims = _run_sweep(monkeypatch, sel, bullets,
                       Adversary(_SHAPES[first], _SHAPES[second]))

    assert len(trims) == 1                    # the driver re-trimmed exactly once
    trimmed_before, trimmed_after = trims[0]
    assert trimmed_before == trimmed_after, (
        "the re-trim after the AI-writing sweep cut text, so an over-long rewrite "
        f"reached it: {[gk for gk in trimmed_before if trimmed_before[gk] != trimmed_after[gk]]}")


# ── the two ends of the acceptance check, over the same fixture ──────────────────
def test_an_overflowing_rewrite_leaves_every_bullet_byte_identical(
        pinned_engine, stub_template_head, monkeypatch):
    """An item whose rewrite overflows twice ships the text it started with.

    The original survives byte for byte, which is a stronger claim than "the bullet still
    fits". A clause-boundary cut through freshly written text is where a mid-sentence
    bullet comes from, so this design never trims a rewrite; it discards it.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    before = dict(bullets)
    adversary = Adversary(_much_longer, _much_longer)
    _run_sweep(monkeypatch, sel, bullets, adversary)

    assert bullets == before
    # One item call plus one re-ask for each of the four items, and no third call.
    assert [r for r, _item in adversary.rounds].count("sweep") == 4
    assert [r for r, _item in adversary.rounds].count("reask") == 4


def test_a_fitting_rewrite_is_committed(pinned_engine, stub_template_head, monkeypatch):
    """The fitting shape really does reach the page, and the re-ask never fires.

    Without this the file could pass with an acceptance check that refuses everything,
    which would keep the page safe by making the whole stage a no-op.
    """
    sel, bullets = _pre_sweep(monkeypatch)
    before = dict(bullets)
    adversary = Adversary(_shorter, _shorter)
    _run_sweep(monkeypatch, sel, bullets, adversary)

    assert bullets != before
    assert all(bullets[gk] != before[gk] for gk in before
               if not compose.is_verbatim_gkey(gk))
    assert [r for r, _item in adversary.rounds] == ["sweep"] * 4
