"""The item-level AI-writing sweep (`resume_tailor.sweep`).

One model call per ITEM (one Experience / Projects / Leadership entry with all of
its bullets), because the tells this feature chases only exist across an item's
bullets. `itemcheck` finds them, `aiwriting` says what a resume bullet may not
say, and this module is what turns a finding into a repaired bullet without
moving the page.

**Nothing here calls a model.** `sweep.call` is monkeypatched in every test, and
the recorder counts what it was asked for. A test that reaches the real transport
would place a billed request, so the fixture below installs the stub before the
sweep is ever entered and every test drives it through that fixture.

Three tests carry the feature's guarantees, and each one is a layout or a
grounding promise the rest of the pipeline is allowed to assume:

* `test_an_over_long_rewrite_is_rejected_and_the_original_survives_byte_identical`
  is the layout invariant. A committed rewrite always renders within the bullet's
  own printed-line budget, so the item's total line count never grows and
  `compile.enforce_one_page` can never be pushed over by this pass.
* `test_the_re_ask_fires_exactly_once_and_only_for_the_overflowing_bullets` is the
  cost and termination bound. Two calls per item at the very most, and the second
  one carries only the bullets the first one made too long.
* `test_a_rewrite_that_drops_a_number_is_not_committed` (with its atom-token twin)
  is the grounding promise in the direction the existing gate does not cover:
  `verify.enforce_grounded` reverts a bullet that INVENTS a token, and this
  rejects one that LOSES a token it was already carrying.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import (aiwriting, assets, compose, config,  # noqa: E402
                           itemcheck, measure, sweep)

ITEM = "Example Corp"

# The atoms behind the three bullets. Only the sweep's PAYLOAD reads these (the
# acceptance check compares a rewrite against the text it replaces, never against
# the master), so they carry just enough to be recognisable in a prompt body.
_ATOMS = {
    "a1": {"id": "a1",
           "what": "batched async fetcher for the nightly ingestion job",
           "impact": "runtime 6 hours to 90 minutes across 12 source systems"},
    "a2": {"id": "a2",
           "what": "pytest suites for the three least-covered billing modules",
           "impact": "coverage 42% to 81%, 3 regressions caught before release"},
    "a3": {"id": "a3",
           "what": "migration of 12 engineers to trunk-based development",
           "impact": "mean time to merge 3 days to 6 hours"},
    "v1": {"id": "v1", "what": "unused"},
}

# The clean item, borrowed from tests/test_item_detectors.py: three bullets of the
# shape this pipeline really produces, sized against a 2-line budget, on which no
# detector fires. Single-atom groups, so a gkey is its atom id.
CLEAN = {
    "a1": ("Built a batched async fetcher for the nightly ingestion job, cutting "
           "runtime from 6 hours to 90 minutes across 12 source systems and removing "
           "about $400 a month of redundant cloud spend."),
    "a2": ("Raised test coverage on the billing service from 42% to 81% with pytest "
           "suites for its three least-covered modules, which caught 3 regressions "
           "before release."),
    "a3": ("Led the migration of 12 engineers to trunk-based development and cut mean "
           "time to merge from 3 days to 6 hours."),
}


def _sel(groups=(["a1"], ["a2"], ["a3"]), section="experience"):
    sel = {"experience": [], "projects": [], "leadership": []}
    sel[section].append({"name": ITEM, "groups": [list(g) for g in groups]})
    return sel


# ── the transport stub ───────────────────────────────────────────────────────
_REASK_MARK = "came back too long"


class Recorder:
    """A `sweep.call` replacement that records every prompt it is handed.

    `first` answers the item call and `second` answers the bounded re-ask. A
    third call has nowhere to go and raises, so "never a third call" is enforced
    by the stub itself rather than only by an assertion after the fact.
    """

    def __init__(self, first=None, second=None, raises=False):
        self.first, self.second, self.raises = first, second, raises
        self.calls = []          # [(kind, system, user), ...]

    def __call__(self, system, user, tier, **kw):
        kind = "reask" if _REASK_MARK in system else "sweep"
        self.calls.append((kind, system, user))
        if self.raises:
            raise RuntimeError("transport down")
        if kind == "reask":
            if self.second is None:
                raise AssertionError("the sweep issued an unexpected re-ask")
            return self.second
        if self.first is None:
            raise AssertionError("the sweep issued an unexpected item call")
        return self.first

    @property
    def kinds(self):
        return [k for k, _, _ in self.calls]

    def payload(self, n):
        """The bullets array of call `n`'s user prompt, parsed back out of it."""
        _, _, user = self.calls[n]
        start = user.index('{\n  "item"') if '{\n  "item"' in user else user.index("{")
        depth, end = 0, None
        for i, ch in enumerate(user[start:], start):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                end = i + 1
                break
        return json.loads(user[start:end])


def _answer(**texts):
    return {"bullets": [{"gkey": gk, "text": t} for gk, t in texts.items()]}


@pytest.fixture()
def engine(monkeypatch):
    """Every input the sweep reads, pinned. No user data, no config.json, no model."""
    monkeypatch.setattr(assets, "atoms_by_id", lambda: {k: dict(v)
                                                        for k, v in _ATOMS.items()})
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)


def _install(monkeypatch, recorder):
    monkeypatch.setattr(sweep, "call", recorder)
    return recorder


# ── the fixture itself has to be honest ──────────────────────────────────────
def test_the_clean_bullets_fit_their_own_budget(engine):
    """Every assertion below rests on this: the starting text is already legal."""
    for text in CLEAN.values():
        assert measure.line_count(text) <= 2
    assert measure.char_budget(2) == 245


def test_no_detector_fires_on_the_clean_item(engine):
    assert itemcheck.item_findings(ITEM, list(CLEAN.items())) == []


# ── the toggle is "on unconditionally" ───────────────────────────────────────
def test_a_clean_item_still_makes_its_call_and_commits_nothing(engine, monkeypatch):
    """The frozen answer is on unconditionally: a clean item is still sent, because
    the detectors are deterministic and the judgment arm is what they cannot see."""
    rec = _install(monkeypatch, Recorder(first=_answer(**CLEAN)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert rec.kinds == ["sweep"]
    assert bullets == CLEAN
    assert result.changed == ()
    assert result.rejected == ()
    assert result.calls == 1


def test_one_call_per_item(engine, monkeypatch):
    sel = _sel()
    sel["projects"].append({"name": "Trailhead", "groups": [["v1"]]})
    rec = _install(monkeypatch, Recorder(first={"bullets": []}))
    bullets = dict(CLEAN)
    bullets["v1"] = "Designed a hiking route planner that ranks 40 trails by weather."
    result = sweep.sweep_items("", "Data Engineer", sel, bullets)
    assert rec.kinds == ["sweep", "sweep"]
    assert result.items == 2


def test_an_item_with_no_live_bullets_is_not_sent(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first=None))
    result = sweep.sweep_items("", "Data Engineer", _sel(), {})
    assert rec.calls == []
    assert result.calls == 0


# ── the happy path ───────────────────────────────────────────────────────────
_REPAIRED_A1 = ("Built a batched async fetcher for the nightly ingestion job that cut "
                "runtime from 6 hours to 90 minutes across 12 source systems and "
                "removed about $400 a month of cloud spend.")


def test_a_targeted_rewrite_is_committed(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first=_answer(a1=_REPAIRED_A1, a2=CLEAN["a2"],
                                                       a3=CLEAN["a3"])))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a1"] == _REPAIRED_A1
    assert bullets["a2"] == CLEAN["a2"] and bullets["a3"] == CLEAN["a3"]
    assert result.changed == ("a1",)
    assert result.rejected == ()
    assert rec.kinds == ["sweep"]


def test_an_unchanged_return_is_neither_a_change_nor_a_rejection(engine, monkeypatch):
    _install(monkeypatch, Recorder(first=_answer(a1=CLEAN["a1"] + "  ")))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets == CLEAN
    assert (result.changed, result.rejected) == ((), ())


def test_a_bullet_missing_from_the_response_keeps_its_text(engine, monkeypatch):
    _install(monkeypatch, Recorder(first=_answer(a1=_REPAIRED_A1)))
    bullets = dict(CLEAN)
    sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a2"] == CLEAN["a2"] and bullets["a3"] == CLEAN["a3"]


def test_a_response_of_the_wrong_shape_changes_nothing(engine, monkeypatch):
    _install(monkeypatch, Recorder(first={"bullets": ["not an object", 7, None]}))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets == CLEAN
    assert result.changed == ()


# ── GUARANTEE 1: an over-long rewrite is rejected, byte for byte ─────────────
_OVERLONG_A1 = ("Built a batched async fetcher for the nightly ingestion job that cut "
                "runtime from 6 hours to 90 minutes across 12 source systems, removed "
                "about $400 a month of cloud spend, and left the on-call rotation with "
                "one fewer nightly page to answer while the backfill window shrank to "
                "a single quiet hour before dawn each weekday morning.")


def test_the_overlong_fixture_really_does_overflow(engine):
    assert measure.line_count(_OVERLONG_A1) > 2


def test_an_over_long_rewrite_is_rejected_and_the_original_survives_byte_identical(
        engine, monkeypatch):
    """The layout invariant. A rewrite that would print on a third line is thrown
    away and the original text is returned untouched, character for character."""
    original = CLEAN["a1"]
    rec = _install(monkeypatch, Recorder(
        first=_answer(a1=_OVERLONG_A1),
        second=_answer(a1=_OVERLONG_A1)))       # the re-ask fails the same way
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)

    assert bullets["a1"] == original            # byte-identical
    assert bullets["a1"] is original            # and literally the same object
    assert result.changed == ()
    assert [r.reason for r in result.rejected] == [sweep.REASON_OVERFLOW]
    assert result.rejected[0].gkey == "a1"
    assert rec.kinds == ["sweep", "reask"]      # never a third


def test_every_committed_rewrite_renders_within_its_budget(engine, monkeypatch):
    """The invariant stated as a property over the whole item: whatever the sweep
    leaves behind, no bullet is over its own printed-line target."""
    _install(monkeypatch, Recorder(first=_answer(a1=_OVERLONG_A1, a2=_REPAIRED_A1,
                                                 a3=CLEAN["a3"]),
                                   second=_answer(a1=_OVERLONG_A1)))
    bullets = dict(CLEAN)
    sel = _sel()
    sweep.sweep_items("", "Data Engineer", sel, bullets)
    targets = compose.bullet_line_targets(sel)
    for gk, text in bullets.items():
        assert measure.line_count(text) <= targets[gk], gk


# ── GUARANTEE 2: the bounded re-ask ─────────────────────────────────────────
_SHORTENED_A1 = ("Built a batched async fetcher for the nightly ingestion job that cut "
                 "runtime from 6 hours to 90 minutes across 12 source systems and "
                 "saved about $400 a month of cloud spend.")


def test_the_re_ask_fires_exactly_once_and_only_for_the_overflowing_bullets(
        engine, monkeypatch):
    """One further call per item, carrying only the bullets that overflowed. The
    bullet rejected for a changed opening verb is NOT re-asked: the re-ask exists
    to buy back length, and nothing else."""
    verb_swap = CLEAN["a3"].replace("Led", "Drove", 1)
    rec = _install(monkeypatch, Recorder(
        first=_answer(a1=_OVERLONG_A1, a2=CLEAN["a2"], a3=verb_swap),
        second=_answer(a1=_SHORTENED_A1)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)

    assert rec.kinds == ["sweep", "reask"]
    assert len(rec.calls) == 2
    sent = {b["gkey"] for b in rec.payload(1)["bullets"]}
    assert sent == {"a1"}
    assert bullets["a1"] == _SHORTENED_A1
    assert bullets["a3"] == CLEAN["a3"]
    assert result.changed == ("a1",)
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a3", sweep.REASON_VERB)]
    assert result.reasked == (ITEM,)


def test_the_re_ask_names_the_exact_character_overage(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first=_answer(a1=_OVERLONG_A1),
                                         second=_answer(a1=_SHORTENED_A1)))
    sweep.sweep_items("", "Data Engineer", _sel(), dict(CLEAN))
    entry = rec.payload(1)["bullets"][0]
    assert entry["gkey"] == "a1"
    assert entry["rewritten"] == _OVERLONG_A1
    assert entry["original"] == CLEAN["a1"]
    assert entry["max_chars"] == 245
    assert entry["too_long_by"] == len(_OVERLONG_A1) - sweep._fitting_prefix_len(
        _OVERLONG_A1, 2)
    assert entry["too_long_by"] > 0


def test_the_re_ask_result_faces_the_same_acceptance(engine, monkeypatch):
    """A shortened rewrite that drops a fact is discarded like any other."""
    lossy = ("Built a batched async fetcher for the nightly ingestion job that cut "
             "runtime from 6 hours to 90 minutes and saved cloud spend.")
    rec = _install(monkeypatch, Recorder(first=_answer(a1=_OVERLONG_A1),
                                         second=_answer(a1=lossy)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a1"] == CLEAN["a1"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a1", sweep.REASON_FACTS)]
    assert len(rec.calls) == 2


def test_no_re_ask_when_nothing_overflowed(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first=_answer(a1=_REPAIRED_A1), second=None))
    result = sweep.sweep_items("", "Data Engineer", _sel(), dict(CLEAN))
    assert rec.kinds == ["sweep"]
    assert result.reasked == ()


def test_a_failed_re_ask_leaves_the_originals(engine, monkeypatch):
    class Flaky(Recorder):
        def __call__(self, system, user, tier, **kw):
            if _REASK_MARK in system:
                self.calls.append(("reask", system, user))
                raise RuntimeError("transport down")
            return super().__call__(system, user, tier, **kw)

    rec = _install(monkeypatch, Flaky(first=_answer(a1=_OVERLONG_A1)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets == CLEAN
    assert rec.kinds == ["sweep", "reask"]
    assert result.failures == (ITEM,)


# ── GUARANTEE 3: a rewrite may not lose what it was carrying ────────────────
def test_a_rewrite_that_drops_a_number_is_not_committed(engine, monkeypatch):
    lossy = ("Built a batched async fetcher for the nightly ingestion job, cutting "
             "runtime from 6 hours to 90 minutes across the source systems and "
             "removing about $400 a month of redundant cloud spend.")
    assert measure.line_count(lossy) <= 2       # it fits; only the fact is missing
    _install(monkeypatch, Recorder(first=_answer(a1=lossy)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a1"] == CLEAN["a1"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a1", sweep.REASON_FACTS)]
    assert "12" in result.rejected[0].detail


def test_a_rewrite_that_drops_an_atom_token_is_not_committed(engine, monkeypatch):
    original = ("Raised PostgreSQL replica lag from 9 seconds to 400 milliseconds by "
                "rebuilding the logical decoding slot on the billing cluster.")
    lossy = ("Raised replica lag from 9 seconds to 400 milliseconds by rebuilding the "
             "logical decoding slot on the billing cluster.")
    _install(monkeypatch, Recorder(first=_answer(a2=lossy)))
    bullets = dict(CLEAN, a2=original)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a2"] == original
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a2", sweep.REASON_FACTS)]
    assert "PostgreSQL" in result.rejected[0].detail


# ── each acceptance condition rejects on its own ────────────────────────────
def test_an_empty_rewrite_is_rejected(engine, monkeypatch):
    _install(monkeypatch, Recorder(first={"bullets": [{"gkey": "a1", "text": "   "}]}))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a1"] == CLEAN["a1"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a1", sweep.REASON_EMPTY)]


def test_a_new_style_violation_is_rejected(engine, monkeypatch):
    """compose.style_violations: the deterministic per-bullet gate's own list."""
    slop = CLEAN["a3"].replace("trunk-based development",
                               "seamless trunk-based development")
    assert compose.style_violations(slop) and not compose.style_violations(CLEAN["a3"])
    _install(monkeypatch, Recorder(first=_answer(a3=slop)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a3"] == CLEAN["a3"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a3", sweep.REASON_STYLE)]
    assert "hollow intensifier" in result.rejected[0].detail


def test_a_new_resume_violation_is_rejected(engine, monkeypatch):
    """aiwriting.resume_violations: the SP1 arm, disjoint from the list above."""
    slop = CLEAN["a3"].replace("Led the migration",
                               "Led the industry-leading migration")
    assert aiwriting.resume_violations(slop) == ["promotional language"]
    assert aiwriting.resume_violations(CLEAN["a3"]) == []
    _install(monkeypatch, Recorder(first=_answer(a3=slop)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a3"] == CLEAN["a3"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a3", sweep.REASON_STYLE)]
    assert "promotional language" in result.rejected[0].detail


def test_a_violation_the_original_already_had_is_not_a_new_one(engine, monkeypatch):
    """Only a NEW violation blocks a commit. A repair that removes one tell while
    carrying an older one forward still lands, exactly as enforce_style commits on
    strict improvement."""
    dirty = "Led a robust migration of 12 engineers to trunk-based development."
    repaired = "Led a robust migration of 12 engineers to trunk-based delivery."
    _install(monkeypatch, Recorder(first=_answer(a3=repaired)))
    bullets = dict(CLEAN, a3=dirty)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a3"] == repaired
    assert result.changed == ("a3",)


def test_a_changed_opening_verb_is_rejected(engine, monkeypatch):
    swapped = CLEAN["a2"].replace("Raised", "Lifted", 1)
    _install(monkeypatch, Recorder(first=_answer(a2=swapped)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a2"] == CLEAN["a2"]
    assert [(r.gkey, r.reason) for r in result.rejected] == [("a2", sweep.REASON_VERB)]


def test_the_pinned_verb_ignores_case_and_edge_punctuation(engine, monkeypatch):
    """`itemcheck.leading_verb` normalizes, so a re-cased opener is the same verb
    and must not be rejected for a difference the renderer cannot show."""
    same = CLEAN["a2"].replace("Raised", "Raised,", 1)
    assert itemcheck.leading_verb(same) == itemcheck.leading_verb(CLEAN["a2"])
    _install(monkeypatch, Recorder(first=_answer(a2=same)))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a2"] == same
    assert result.changed == ("a2",)


# ── verbatim ─────────────────────────────────────────────────────────────────
def test_verbatim_bullets_are_never_sent_and_never_modified(engine, monkeypatch):
    """The user opted into their own text with "use my exact bullets". Every other
    pass in this pipeline skips those, and so does this one."""
    vgk = "__verbatim__/Example Corp/0"
    assert compose.is_verbatim_gkey(vgk)
    rec = _install(monkeypatch, Recorder(
        first=_answer(**{vgk: "Rewritten by the model.", "a1": _REPAIRED_A1})))
    bullets = dict(CLEAN)
    bullets[vgk] = "My own bullet, typed by hand, with an em dash - and all."
    before = bullets[vgk]
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets[vgk] == before
    assert vgk not in rec.calls[0][2]
    assert vgk not in [r.gkey for r in result.rejected]
    assert result.changed == ("a1",)


def test_a_fully_verbatim_item_is_never_sent(engine, monkeypatch):
    """`_blocks_in_order` drops verbatim groups, so an all-verbatim entry has no
    bullets to sweep and buys no call at all."""
    sel = _sel(groups=[["__verbatim__/Example Corp/0"]])
    rec = _install(monkeypatch, Recorder(first=None))
    bullets = {"__verbatim__/Example Corp/0": "My own bullet."}
    result = sweep.sweep_items("", "Data Engineer", sel, bullets)
    assert rec.calls == []
    assert result.calls == 0


# ── failure is advisory ──────────────────────────────────────────────────────
def test_a_raised_call_leaves_every_bullet_untouched(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(raises=True))
    bullets = dict(CLEAN)
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets == CLEAN
    assert result.changed == () and result.rejected == ()
    assert result.failures == (ITEM,)
    assert len(rec.calls) == 1


def test_one_item_failing_does_not_stop_the_next(engine, monkeypatch):
    sel = _sel()
    sel["projects"].append({"name": "Trailhead", "groups": [["v1"]]})
    ok = "Designed a hiking route planner that ranks 40 trails by weather."
    better = "Designed a hiking planner that ranks 40 trails by the weather window."

    class First(Recorder):
        def __call__(self, system, user, tier, **kw):
            self.calls.append(("sweep", system, user))
            if ITEM in user:
                raise RuntimeError("transport down")
            return _answer(v1=better)

    rec = _install(monkeypatch, First())
    bullets = dict(CLEAN, v1=ok)
    result = sweep.sweep_items("", "Data Engineer", sel, bullets)
    assert len(rec.calls) == 2
    assert bullets["v1"] == better
    assert result.failures == (ITEM,)
    assert result.changed == ("v1",)


# ── the findings ride into the payload, and P2 comes back unfixed ───────────
_BARE = "Stable pipeline throughput across 12 source systems."


def test_p1_findings_ride_in_the_payload(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first={"bullets": []}))
    bullets = dict(CLEAN, a1=_BARE)
    sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    body = rec.payload(0)
    detectors = {f["detector"] for f in body["findings"]}
    assert "bare_noun_bullet" in detectors
    assert all(f["tier"] == itemcheck.P1 for f in body["findings"])
    by_gkey = {b["gkey"]: b for b in body["bullets"]}
    assert by_gkey["a1"]["findings"] == ["bare_noun_bullet"]
    assert by_gkey["a2"]["findings"] == []


def test_p2_findings_are_reported_and_never_sent(engine, monkeypatch):
    """The frozen strictness answer: P0 and P1 are repaired, P2 is surfaced in the
    run report and left alone. So a P2 finding never reaches the model."""
    three = {
        "a1": ("Built a batched async fetcher, a retry queue, and a nightly backfill "
               "for the ingestion job across 12 source systems."),
        "a2": ("Raised coverage on the billing service to 81% with unit, contract, "
               "and integration suites for its three least-covered modules."),
        "a3": CLEAN["a3"],
    }
    findings = itemcheck.item_findings(ITEM, list(three.items()))
    assert [f.detector for f in findings if f.tier == itemcheck.P2] == ["rule_of_three"]

    rec = _install(monkeypatch, Recorder(first={"bullets": []}))
    result = sweep.sweep_items("", "Data Engineer", _sel(), dict(three))
    assert [f["tier"] for f in rec.payload(0)["findings"]] == []
    assert [f.detector for f in result.unfixed_p2] == ["rule_of_three"]
    assert all(f.tier == itemcheck.P2 for f in result.unfixed_p2)


def test_the_payload_carries_atoms_budgets_and_line_counts(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first={"bullets": []}))
    sweep.sweep_items("", "Data Engineer", _sel(), dict(CLEAN))
    body = rec.payload(0)
    assert body["item"] == ITEM
    entry = {b["gkey"]: b for b in body["bullets"]}["a1"]
    assert entry["text"] == CLEAN["a1"]
    assert entry["max_chars"] == measure.char_budget(2)
    assert entry["max_lines"] == 2
    assert entry["lines"] == measure.line_count(CLEAN["a1"])
    assert entry["atoms"]["a1"]["what"] == _ATOMS["a1"]["what"]
    assert "_section" not in entry["atoms"]["a1"]


def test_the_prompt_pins_the_opening_verb_and_the_budget(engine, monkeypatch):
    rec = _install(monkeypatch, Recorder(first={"bullets": []}))
    sweep.sweep_items("", "Data Engineer", _sel(), dict(CLEAN))
    system = rec.calls[0][1]
    assert "OPENING VERB" in system
    assert "max_chars" in system
    assert compose.BANNED_PHRASING in system
    assert aiwriting.RESUME_RULES_PROMPT in system


# ── the prompts obey the rules they state ────────────────────────────────────
# tests/test_prompt_hygiene.py scans the whole package once this module is wired
# to a call site. This is the same scan run directly over this module's own
# prompt constants, so a bad edit here fails in the file that caused it.
_PROMPT_CONSTANTS = ("_SWEEP_SYSTEM", "_REASK_SYSTEM")


@pytest.mark.parametrize("name", _PROMPT_CONSTANTS)
def test_the_prompts_are_free_of_the_characters_they_ban(name):
    text = getattr(sweep, name)
    assert "—" not in text
    assert " -- " not in text


@pytest.mark.parametrize("name", _PROMPT_CONSTANTS)
def test_the_prompts_are_free_of_the_phrasing_they_ban(name):
    """The ban enumerations they embed are exempt (a list cannot forbid a phrase
    without quoting it), so only this module's own words are checked."""
    own = getattr(sweep, name)
    for enumeration in (compose.BANNED_PHRASING, aiwriting.RESUME_RULES_PROMPT):
        own = own.replace(enumeration, "")
    assert compose.style_violations(own) == []


# ── the result is what SP4 has to report from ────────────────────────────────
def test_the_result_carries_everything_the_run_report_needs(engine, monkeypatch):
    _install(monkeypatch, Recorder(first=_answer(a1=_OVERLONG_A1, a2=_REPAIRED_A1),
                                   second=_answer(a1=_SHORTENED_A1)))
    result = sweep.sweep_items("", "Data Engineer", _sel(), dict(CLEAN))
    assert set(result._fields) >= {"changed", "rejected", "reasked", "unfixed_p2",
                                   "calls", "items", "failures"}
    assert result.calls == 2
    assert result.items == 1
    assert result.reasked == (ITEM,)
    assert isinstance(result.changed, tuple)


def test_findings_payload_round_trips_the_unfixed_p2(engine, monkeypatch):
    """SP4 reports these, so they have to survive json.dumps."""
    three = {
        "a1": ("Built a batched async fetcher, a retry queue, and a nightly backfill "
               "for the ingestion job across 12 source systems."),
        "a2": ("Raised coverage on the billing service to 81% with unit, contract, "
               "and integration suites for its three least-covered modules."),
        "a3": CLEAN["a3"],
    }
    _install(monkeypatch, Recorder(first={"bullets": []}))
    result = sweep.sweep_items("", "Data Engineer", _sel(), dict(three))
    dumped = json.loads(json.dumps(itemcheck.findings_payload(result.unfixed_p2)))
    assert dumped and dumped[0]["tier"] == itemcheck.P2


# ── the per-bullet phrasing arm reaches the model ────────────────────────────
# Regression tests for a real production defect. The first live run swept 8 items
# in 8 calls and rewrote nothing, and one of the bullets it left alone opened with
# "Assisted with", which aiwriting.resume_violations flags as a formulaic opening.
# The rule fired, and the model was never told: the payload carried only the
# item-level itemcheck findings, and the prompt said to repair what the findings
# flag. The model was following its instructions exactly.
_FORMULAIC = ("Assisted with building a batched async fetcher for the nightly "
              "ingestion job, cutting runtime from 6 hours to 90 minutes across 12 "
              "source systems.")


def test_the_fixture_bullet_really_is_flagged_and_really_does_fit():
    """Both halves matter: a bullet that did not fit would be rejected for length
    instead, and the test would pass while proving nothing about phrasing."""
    assert aiwriting.resume_violations(_FORMULAIC) == ["formulaic opening"]
    assert measure.line_count(_FORMULAIC) <= 2
    # No item-level detector fires on it, so `findings` alone would leave it silent.
    assert itemcheck.item_findings(ITEM, [("a1", _FORMULAIC)]) == []


def test_a_bullet_with_a_phrasing_hit_is_named_in_the_payload(engine, monkeypatch):
    """The defect itself: the model has to be TOLD the rule fired."""
    bullets = dict(CLEAN, a1=_FORMULAIC)
    rec = _install(monkeypatch, Recorder(first=_answer()))
    sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    sent = {b["gkey"]: b for b in rec.payload(0)["bullets"]}
    assert sent["a1"]["phrasing"] == ["formulaic opening"]
    # And a clean bullet in the same item still carries an empty list, so the model
    # can tell the two apart rather than treating the whole entry as suspect.
    assert sent["a2"]["phrasing"] == []


def test_a_declined_phrasing_repair_is_reported_rather_than_silent(engine, monkeypatch):
    """The model may decline. What it may not do is decline invisibly: a run that
    changed nothing used to be indistinguishable from a run that found nothing."""
    bullets = dict(CLEAN, a1=_FORMULAIC)
    _install(monkeypatch, Recorder(first=_answer(a1=_FORMULAIC)))
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert result.changed == ()
    assert result.rejected == ()          # nothing was refused; it was never repaired
    assert [(x.gkey, x.names) for x in result.unfixed_phrasing] == [
        ("a1", ("formulaic opening",))]


def test_a_committed_phrasing_repair_leaves_nothing_lingering(engine, monkeypatch):
    """The other end. A rewrite that clears the rule reports no leftover, so the
    warning cannot cry wolf on a bullet the sweep actually fixed."""
    fixed = ("Assisted engineers by building a batched async fetcher for the nightly "
             "ingestion job, cutting runtime from 6 hours to 90 minutes across 12 "
             "source systems.")
    assert aiwriting.resume_violations(fixed) == []
    bullets = dict(CLEAN, a1=_FORMULAIC)
    _install(monkeypatch, Recorder(first=_answer(a1=fixed)))
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert bullets["a1"] == fixed
    assert result.changed == ("a1",)
    assert result.unfixed_phrasing == ()


def test_a_clean_item_reports_no_lingering_phrasing(engine, monkeypatch):
    """The no-op case stays a no-op: nothing flagged, nothing warned."""
    bullets = dict(CLEAN)
    _install(monkeypatch, Recorder(first=_answer()))
    result = sweep.sweep_items("", "Data Engineer", _sel(), bullets)
    assert result.unfixed_phrasing == ()


def test_the_prompt_tells_the_model_phrasing_is_repairable(engine):
    """The payload field is inert unless the instructions name it. This is the
    other half of the fix and it is the half a refactor would drop."""
    assert "PHRASING" in sweep._SWEEP_SYSTEM
