"""Item-level AI-writing detectors (`resume_tailor.itemcheck`).

`compose.enforce_style` sees one bullet at a time, so it is structurally blind to
every tell that only exists ACROSS an item's bullets. `itemcheck` is the arm that
sees a whole Experience / Projects / Leadership entry at once, and this file is
what pins it.

Nothing here calls a model. Every detector is a pure function over text, so the
whole file is deterministic and free.

The single most load-bearing test is `test_a_realistic_clean_item_is_left_alone`.
Every one of these detectors measures a property that a CORRECT resume item also
has to some degree: bullets in one item are length-matched by the layout engine,
they share the item's subject matter, and they all open on a past-tense verb. A
detector tuned by intuition rather than against that reality fires on text that
was already right, which costs a repair call and can only make the bullet worse.
"""
import ast
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import compose, itemcheck  # noqa: E402

MODULE_PATH = (Path(__file__).resolve().parents[1] / "local" / "resume_tailor"
               / "itemcheck.py")


# ── the clean item ────────────────────────────────────────────────────────────
# Three bullets of the shape this pipeline actually produces: subjectless, opening
# on a distinct past-tense verb, carrying a number, sized against a 2-line budget
# (the real corpus runs 27 to 38 words per bullet). NOTHING may fire on this.
CLEAN_ITEM = [
    ("a1", "Built a batched async fetcher for the nightly ingestion job, cutting "
           "runtime from 6 hours to 90 minutes across 12 source systems and removing "
           "about $400 a month of redundant cloud spend."),
    ("a2", "Raised test coverage on the billing service from 42% to 81% with pytest "
           "suites for its three least-covered modules, which caught 3 regressions "
           "before release."),
    ("a3", "Led the migration of 12 engineers to trunk-based development and cut mean "
           "time to merge from 3 days to 6 hours."),
]

ITEM = "Example Corp"


def _texts(item):
    return [t for _, t in item]


# ── the detector table ────────────────────────────────────────────────────────
# (offending item, near-miss item) per detector, mirroring the shape of
# tests/test_aiwriting_resume.py's RESUME_BAN_CASES. The near-miss must produce NO
# finding from that detector: it is the case a lazily-tuned threshold gets wrong.
DETECTOR_CASES = {
    # Three bullets sharing one skeleton: past-tense -ed opener, participial tail,
    # one clause break, a trailing "by <number>" phrase. The leading verbs are all
    # DIFFERENT, because dedupe_leading_verbs guarantees that upstream, which is
    # exactly why the skeleton cannot be keyed on the verb itself.
    "shape_repetition": (
        [("s1", "Designed a caching layer for the checkout service, reducing p95 "
                "latency by 40%."),
         ("s2", "Automated the nightly report build for the finance team, cutting "
                "manual work by 30%."),
         ("s3", "Refactored the billing importer for the payments group, lowering "
                "error rates by 25%.")],
        # Near miss: only two bullets share the skeleton. The third opens on an
        # irregular verb, carries no participial tail and ends on a noun.
        [("s1", "Designed a caching layer for the checkout service, reducing p95 "
                "latency by 40%."),
         ("s2", "Automated the nightly report build for the finance team, cutting "
                "manual work by 30%."),
         ("s3", "Led a two-week audit of the billing importer that removed 4 dead "
                "endpoints.")],
    ),
    # Metronomic bullet lengths: 21, 21, 21 words.
    "length_uniformity": (
        [("u1", "Built a caching layer for the checkout service and cut the median "
                "response time from 900 ms to 210 ms."),
         ("u2", "Designed a retry policy for the payments worker and cut the failed "
                "charge rate from 3% to under 1%."),
         ("u3", "Automated the nightly report build for the finance team and cut the "
                "manual close from 4 hours to 20 minutes.")],
        # Near miss: the same register, the same subject matter, ordinary variation
        # (13, 21, 30 words). This is what a correct item looks like.
        [("u1", "Built a caching layer that cut median response time to 210 ms."),
         ("u2", "Designed a retry policy for the payments worker and cut the failed "
                "charge rate from 3% to under 1%."),
         ("u3", "Automated the nightly report build for the finance team, which cut "
                "the manual close from 4 hours to 20 minutes and freed two analysts "
                "for the reconciliation backlog.")],
    ),
    # Two "A, B, and C" series in one item. The vendored letter rule allows one.
    "rule_of_three": (
        [("r1", "Built dashboards for the finance, operations, and support teams."),
         ("r2", "Designed the ingestion, validation, and export stages of the "
                "pipeline.")],
        # Near miss: exactly one series, plus a two-item list, which is fine.
        [("r1", "Built dashboards for the finance, operations, and support teams."),
         ("r2", "Designed the ingestion and export stages of the pipeline.")],
    ),
    # Three content nouns repeated in every bullet of the item.
    "noun_cycling": (
        [("n1", "Built the customer pipeline dashboard on Redshift."),
         ("n2", "Designed the customer pipeline dashboard alerts for on-call staff."),
         ("n3", "Refactored the customer pipeline dashboard queries to run in 2 "
                "seconds.")],
        # Near miss: two shared nouns, which is what an item about one system looks
        # like. Real items in the corpus top out at exactly two.
        [("n1", "Built the customer pipeline on Redshift."),
         ("n2", "Designed the customer pipeline alerts for on-call staff."),
         ("n3", "Refactored the customer pipeline queries to run in 2 seconds.")],
    ),
    # A bullet with no finite verb, which asserts nothing.
    "bare_noun_bullet": (
        [("b1", "Built a grading service for 400 weekly submissions."),
         ("b2", "Stable pipeline throughput across every region.")],
        # Near miss: the same claim with a verb in it, and an irregular past tense
        # that a naive "ends in -ed" test would miss.
        [("b1", "Built a grading service for 400 weekly submissions."),
         ("b2", "Cut pipeline restarts from 14 per week to 2.")],
    ),
}


@pytest.mark.parametrize("name", sorted(DETECTOR_CASES))
def test_detector_fires_on_the_offending_item(name):
    offending, _ = DETECTOR_CASES[name]
    found = getattr(itemcheck, name)(ITEM, offending)
    assert found, f"{name} missed its own positive case"
    assert {f.detector for f in found} == {name}
    assert all(f.item == ITEM for f in found)
    assert all(f.spans for f in found), "a finding must carry the offending spans"


@pytest.mark.parametrize("name", sorted(DETECTOR_CASES))
def test_detector_is_silent_on_the_near_miss(name):
    _, near_miss = DETECTOR_CASES[name]
    assert getattr(itemcheck, name)(ITEM, near_miss) == []


@pytest.mark.parametrize("name", sorted(DETECTOR_CASES))
def test_detector_is_silent_on_the_clean_item(name):
    assert getattr(itemcheck, name)(ITEM, CLEAN_ITEM) == []


def test_a_realistic_clean_item_is_left_alone():
    """The most important assertion in the phase.

    A false positive here costs a billed repair call on a bullet that was already
    right, and a repair can only move correct text in the wrong direction. Every
    threshold in the module is calibrated so that this passes.
    """
    assert itemcheck.item_findings(ITEM, CLEAN_ITEM) == []


def test_every_detector_in_the_table_is_registered():
    """A detector that exists but is not in DETECTORS never runs in production."""
    assert {name for name, _ in itemcheck.DETECTORS} == set(DETECTOR_CASES)


def test_item_findings_runs_every_detector():
    combined = []
    for name, _ in itemcheck.DETECTORS:
        offending, _near = DETECTOR_CASES[name]
        combined += [f.detector for f in itemcheck.item_findings(ITEM, offending)]
    assert set(combined) >= set(DETECTOR_CASES)


# ── spans point at real text ─────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(DETECTOR_CASES))
def test_spans_index_the_bullet_they_name(name):
    offending, _ = DETECTOR_CASES[name]
    by_gkey = dict(offending)
    for finding in getattr(itemcheck, name)(ITEM, offending):
        for span in finding.spans:
            assert span.gkey in by_gkey
            assert 0 <= span.start < span.end <= len(by_gkey[span.gkey])
            assert by_gkey[span.gkey][span.start:span.end] == span.text


# ── stability and order independence ─────────────────────────────────────────
ALL_ITEMS = [CLEAN_ITEM] + [item for pair in DETECTOR_CASES.values() for item in pair]


@pytest.mark.parametrize("item", ALL_ITEMS, ids=range(len(ALL_ITEMS)))
def test_findings_are_order_independent(item):
    """Same item, different bullet order, same findings.

    SP3 groups bullets with `compose._blocks_in_order`, and a repair payload that
    changes shape because two bullets swapped places is a payload no test can pin.
    """
    baseline = itemcheck.item_findings(ITEM, item)
    rng = random.Random(11)
    for _ in range(6):
        shuffled = list(item)
        rng.shuffle(shuffled)
        assert itemcheck.item_findings(ITEM, shuffled) == baseline


@pytest.mark.parametrize("item", ALL_ITEMS, ids=range(len(ALL_ITEMS)))
def test_findings_are_repeatable(item):
    assert itemcheck.item_findings(ITEM, item) == itemcheck.item_findings(ITEM, item)


# ── degenerate items ─────────────────────────────────────────────────────────
DEGENERATE = [
    [],
    [("x1", "Built a grading service for 400 weekly submissions.")],
    [("x1", "")],
    [("x1", "   ")],
    [("x1", ""), ("x2", "   ")],
    [("x1", "Built."), ("x2", "Led."), ("x3", "Designed.")],
]


@pytest.mark.parametrize("item", DEGENERATE, ids=range(len(DEGENERATE)))
def test_degenerate_items_do_not_crash(item):
    findings = itemcheck.item_findings(ITEM, item)
    assert isinstance(findings, list)
    for name, fn in itemcheck.DETECTORS:
        assert isinstance(fn(ITEM, item), list), name


def test_an_empty_bullet_is_not_a_bare_noun_bullet():
    """There is nothing to repair in a blank bullet, and an earlier stage drops it.
    Flagging it would send the model an empty span to fix."""
    assert itemcheck.bare_noun_bullet(ITEM, [("x1", "  ")]) == []


def test_the_statistical_detectors_need_three_bullets():
    """Burstiness across two samples is not a measurement. n >= 3 or nothing."""
    two = [("t1", "Built a caching layer that cut latency by 40% for the checkout."),
           ("t2", "Designed a retry policy that cut failures by 40% for the payment.")]
    assert itemcheck.length_uniformity(ITEM, two) == []
    assert itemcheck.noun_cycling(ITEM, two) == []
    assert itemcheck.shape_repetition(ITEM, two) == []


# ── severity tiers ───────────────────────────────────────────────────────────
# The frozen strictness answer for this cycle is "P0 and P1 fixed, P2 reported",
# so every finding has to say which tier it belongs to. The values come from the
# skill's own severity list: synonym cycling and bare-noun bullet lists are P1,
# uniform length and compulsive rule of three are P2.
EXPECTED_TIERS = {
    "shape_repetition": itemcheck.P1,
    "length_uniformity": itemcheck.P2,
    "rule_of_three": itemcheck.P2,
    "noun_cycling": itemcheck.P1,
    "bare_noun_bullet": itemcheck.P1,
}


@pytest.mark.parametrize("name", sorted(DETECTOR_CASES))
def test_every_finding_carries_its_severity_tier(name):
    offending, _ = DETECTOR_CASES[name]
    found = getattr(itemcheck, name)(ITEM, offending)
    assert {f.tier for f in found} == {EXPECTED_TIERS[name]}


def test_the_two_tiers_are_distinct():
    assert itemcheck.P1 != itemcheck.P2


# ── the SP3 payload ──────────────────────────────────────────────────────────
def test_findings_payload_is_json_serialisable():
    """SP3 drops these straight into a prompt payload, so a NamedTuple that only
    survives repr() is not enough."""
    findings = []
    for offending, _ in DETECTOR_CASES.values():
        findings += itemcheck.item_findings(ITEM, offending)
    payload = itemcheck.findings_payload(findings)
    assert isinstance(payload, list) and payload
    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text) == payload
    for row in payload:
        assert set(row) == {"detector", "tier", "item", "detail", "spans"}
        for span in row["spans"]:
            assert set(span) == {"gkey", "start", "end", "text"}


def test_findings_payload_of_nothing_is_an_empty_list():
    assert itemcheck.findings_payload([]) == []


def test_detail_text_obeys_the_projects_own_writing_rules():
    """A detail string rides into an LLM prompt in SP3. A model copies the
    punctuation it is shown, so the same rule the prompts live under applies here:
    tests/test_prompt_hygiene.py bans an em dash in anything a model reads."""
    findings = []
    for offending, near_miss in DETECTOR_CASES.values():
        findings += itemcheck.item_findings(ITEM, offending)
        findings += itemcheck.item_findings(ITEM, near_miss)
    for finding in findings:
        assert "—" not in finding.detail, finding.detail
        assert " -- " not in finding.detail, finding.detail
        assert finding.detail.strip() == finding.detail


# ── the stdlib-only guarantee ────────────────────────────────────────────────
# This is the property that makes the module verifiable on its own, without
# `config.load_dotenv()` pulling live credentials into the process. It already
# paid for itself this cycle: SP1's hedge regex was checked standalone and three
# false negatives fell out that the suite had not covered.
def test_itemcheck_imports_only_the_standard_library():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import pulls in the whole package"
            assert (node.module or "").split(".")[0] in stdlib, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, alias.name


def test_itemcheck_loads_with_no_package_at_all():
    """Executed outside `resume_tailor`, so any `from . import ...` raises here."""
    spec = importlib.util.spec_from_file_location("_itemcheck_standalone", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.leading_verb("Built a scraper that cut per-run cost 65%.") == "built"
    assert module.item_findings(ITEM, CLEAN_ITEM) == []


# ── leading_verb has exactly one definition ──────────────────────────────────
LEADING_VERB_TABLE = (
    ("Built A.", "built"),
    ("Engineered, a pipeline", "engineered"),
    ("   Led   the team", "led"),
    ("Co-developed a tool", "co-developed"),
    ("(Rebuilt) the importer", "rebuilt"),
    ("", ""),
    ("   ", ""),
    ("...", ""),
)


@pytest.mark.parametrize("text,expected", LEADING_VERB_TABLE)
def test_leading_verb_normalises_the_opening_token(text, expected):
    assert itemcheck.leading_verb(text) == expected


def test_compose_leading_verb_is_the_same_object():
    """`leading_verb` moved here so the skeleton in `shape_repetition` and the
    opener dedupe cannot drift apart. `compose.leading_verb` is the historical
    name every other call site and test uses, so it has to keep resolving, and it
    has to resolve to THIS function rather than to a second copy."""
    assert compose.leading_verb is itemcheck.leading_verb


@pytest.mark.parametrize("text,expected", LEADING_VERB_TABLE)
def test_compose_leading_verb_behaves_identically(text, expected):
    assert compose.leading_verb(text) == expected


# ── the calibration is written down ──────────────────────────────────────────
def test_the_uniformity_floor_records_what_it_was_calibrated_against():
    """A bare number here is folklore in one cycle. Every bullet in an item is
    already trimmed to a per-bullet printed-line target, so bullets in one item
    are SUPPOSED to be similar lengths; the floor only means something next to the
    measurement of how similar a correct item actually is."""
    doc = itemcheck.__doc__ or ""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "calibrat" in (doc + source).lower()
    assert "0.03" in source


def test_uniformity_is_measured_relative_to_bullet_length():
    """A one-line item and a two-line item have different absolute spreads for the
    same relative variation, so an absolute word-count floor would fire on one and
    never on the other. Doubling every bullet must not change the verdict."""
    tight = [("v1", " ".join(["word"] * 20) + "."),
             ("v2", " ".join(["token"] * 20) + "."),
             ("v3", " ".join(["item"] * 20) + ".")]
    doubled = [(gk, text + " " + text) for gk, text in tight]
    assert itemcheck.length_uniformity(ITEM, tight)
    assert itemcheck.length_uniformity(ITEM, doubled)


def test_uniformity_ignores_an_item_of_blank_bullets():
    blanks = [("v1", "  "), ("v2", ""), ("v3", " ")]
    assert itemcheck.length_uniformity(ITEM, blanks) == []


# ── noun cycling excludes the openers ────────────────────────────────────────
def test_noun_cycling_ignores_the_leading_verbs():
    """`compose.dedupe_leading_verbs` already guarantees distinct openers, so a
    finding about them would be pure noise. Three bullets whose ONLY shared
    material is their own opening verbs stay clean."""
    item = [("c1", "Built a grading service for 400 weekly submissions."),
            ("c2", "Designed a roster importer used by 30 staff."),
            ("c3", "Automated the payroll export for two campuses.")]
    assert itemcheck.noun_cycling(ITEM, item) == []


def test_noun_cycling_catches_conspicuous_synonym_rotation():
    """The skill's other half: AI either repeats a word mechanically or cycles
    through synonyms. Three members of one synonym set across three bullets, none
    of them an opener, is the rotation."""
    item = [("y1", "Built a grading system for 400 weekly submissions."),
            ("y2", "Designed a review platform used by 12 teaching assistants."),
            ("y3", "Automated a deployment framework behind three services.")]
    found = itemcheck.noun_cycling(ITEM, item)
    assert found
    assert any("synonym" in f.detail.lower() for f in found)


def test_two_synonyms_are_not_a_rotation():
    item = [("y1", "Built a grading system for 400 weekly submissions."),
            ("y2", "Designed a review platform used by 12 teaching assistants."),
            ("y3", "Automated the payroll export for two campuses.")]
    assert itemcheck.noun_cycling(ITEM, item) == []


# ── shape repetition does not key on the opener ──────────────────────────────
def test_shape_repetition_cannot_key_on_the_verb_itself():
    """`dedupe_leading_verbs` guarantees every opener in a resume is distinct, so a
    skeleton that included the verb's identity could never repeat and the detector
    would be dead code. It keys on the verb's FORM instead."""
    offending, _ = DETECTOR_CASES["shape_repetition"]
    verbs = [itemcheck.leading_verb(t) for t in _texts(offending)]
    assert len(set(verbs)) == len(verbs), "the positive case must have distinct openers"
    assert itemcheck.shape_repetition(ITEM, offending)
