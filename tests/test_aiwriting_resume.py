"""The résumé arm of the vendored avoid-ai-writing extract.

`resume_tailor.aiwriting` already carries a cover-letter arm. This file covers the
second arm added for the item-level sweep, and the thing it mostly protects is a
*negative*: a résumé bullet is a subjectless fragment that opens with a past-tense
action verb, and two upstream rules (`Subjectless fragments and agentless passives`,
`Missing first-person perspective`) fire on exactly that shape. Left alone they push
correct bullets toward "I built ...". The profile below turns both off, and the
single most load-bearing assertion in this file is that a correct résumé-register
bullet produces zero findings.

Nothing here calls a model. `resume_violations()` is a pure function over regexes and
`RESUME_RULES_PROMPT` is a constant, so the whole file is deterministic and free.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

import test_prompt_hygiene as hygiene  # noqa: E402 - sibling test module, no pkg import

from resume_tailor import aiwriting, compose  # noqa: E402


# The bullet the whole phase exists to protect: correct register, correct grammar,
# a number, no slop. Every gate in this module must leave it completely alone.
CLEAN_BULLET = "Built a scraper that cut per-run cost 65%."


# ── attribution ───────────────────────────────────────────────────────────────
def test_module_docstring_still_credits_the_skill():
    """Adding the résumé arm must not push the vendor credit out of the docstring."""
    doc = aiwriting.__doc__ or ""
    for token in ("avoid-ai-writing", "3.18.0", "MIT", "Conor Bronsdon", "SKILL.md"):
        assert token in doc, token


def test_the_letter_arm_is_untouched():
    """The two arms share a module so the tier-1 vocabulary cannot drift into a
    second copy. That only holds if adding the résumé arm changes nothing about the
    letter arm's public surface."""
    assert [name for name, _ in aiwriting.EXTRA_BANS] == [
        "tier-1 vocabulary", "chatbot artifact", "let's opener", "era framing",
        "confidence calibration", "generic conclusion"]
    assert aiwriting.violations(
        "I want to delve into your compiler work.") == ["tier-1 vocabulary"]
    assert "avoid-ai-writing v3.18.0" in aiwriting.RULES_PROMPT


# ── (a) the résumé tolerance profile ─────────────────────────────────────────
# The upstream tolerance matrix has six columns (linkedin / blog / technical-blog /
# investor-email / docs / casual) and 22 rows. The résumé profile is a seventh
# column over those same rows, plus one row the matrix does not carry
# ("missing first-person perspective", which lives under Rhythm and uniformity).
UPSTREAM_MATRIX_ROWS = (
    "em dashes",
    "bold overuse",
    "emoji in headers",
    "excessive bullets",
    "hedging",
    "word table (full list)",
    "promotional language",
    "significance inflation",
    "copula avoidance",
    "uniform paragraph length",
    "numbered list inflation",
    "rhetorical questions",
    "transition phrases",
    "generic conclusions",
    "hashtag stuffing",
    "bullet-np lists",
    "tier 3 phrase clustering",
    "future-narrative closers",
    "social endorsement closers",
    "hedge-stacked predictions",
    "real/actual inflation",
    "subjectless fragments and agentless passives",
)

LEVELS = {aiwriting.SKIP, aiwriting.RELAXED, aiwriting.STRICT,
          aiwriting.EXTRA_STRICT, aiwriting.DELEGATED}


def test_profile_is_the_upstream_matrix_plus_the_first_person_row():
    expected = set(UPSTREAM_MATRIX_ROWS) | {"missing first-person perspective"}
    assert set(aiwriting.RESUME_PROFILE) == expected


def test_every_profile_entry_carries_a_level_and_a_written_reason():
    for rule, tol in aiwriting.RESUME_PROFILE.items():
        assert tol.level in LEVELS, rule
        # A bare "skip" with no reason is how a deviation from the upstream skill
        # silently becomes folklore. Every row states why in the code itself.
        assert len(tol.why) >= 40, rule


def test_the_five_levels_are_distinct_strings():
    assert len(LEVELS) == 5
    assert all(isinstance(level, str) for level in LEVELS)


# Asserted one by one, so a later edit cannot quietly re-enable one of them.
SKIPPED_RULES = (
    "subjectless fragments and agentless passives",
    "missing first-person perspective",
    "copula avoidance",
    "transition phrases",
    "generic conclusions",
    "hashtag stuffing",
    "excessive bullets",
    "bold overuse",
    "emoji in headers",
    "numbered list inflation",
    "rhetorical questions",
    "future-narrative closers",
    "social endorsement closers",
)


@pytest.mark.parametrize("rule", SKIPPED_RULES)
def test_rule_is_skipped_for_the_resume_profile(rule):
    assert aiwriting.RESUME_PROFILE[rule].level == aiwriting.SKIP


def test_subjectless_fragments_are_skipped_because_that_form_is_the_register():
    tol = aiwriting.RESUME_PROFILE["subjectless fragments and agentless passives"]
    assert tol.level == aiwriting.SKIP
    assert "register" in tol.why.lower()


def test_missing_first_person_is_skipped_and_the_reason_records_the_inversion():
    """The one row where this profile deliberately contradicts the upstream skill:
    upstream reads the absence of "I think" as a tell, and a résumé forbids first
    person outright. The reason has to say so in the code."""
    tol = aiwriting.RESUME_PROFILE["missing first-person perspective"]
    assert tol.level == aiwriting.SKIP
    why = tol.why.lower()
    assert "invert" in why
    assert "first person" in why


@pytest.mark.parametrize("rule", ["promotional language", "significance inflation",
                                  "bullet-np lists", "hedge-stacked predictions"])
def test_rule_is_extra_strict_for_the_resume_profile(rule):
    assert aiwriting.RESUME_PROFILE[rule].level == aiwriting.EXTRA_STRICT


def test_hedging_is_strict():
    assert aiwriting.RESUME_PROFILE["hedging"].level == aiwriting.STRICT


@pytest.mark.parametrize("rule", ["uniform paragraph length", "em dashes",
                                  "tier 3 phrase clustering"])
def test_rule_is_delegated_rather_than_skipped(rule):
    """Delegated is not skipped: the signal is still audited, by a component that
    can see the right unit of text. Uniform paragraph length becomes uniform BULLET
    length across an item, which needs the whole item and cannot be read off
    one bullet."""
    assert aiwriting.RESUME_PROFILE[rule].level == aiwriting.DELEGATED


def test_uniform_paragraph_length_reason_names_the_bullet_reinterpretation():
    why = aiwriting.RESUME_PROFILE["uniform paragraph length"].why.lower()
    assert "bullet" in why


def test_profile_level_defaults_to_strict_for_an_unlisted_rule():
    """Upstream: "Rules not listed in the table apply at full strength"."""
    assert aiwriting.resume_profile_level("chatbot artifacts") == aiwriting.STRICT
    assert aiwriting.resume_profile_level("hedging") == aiwriting.STRICT
    assert aiwriting.resume_profile_level("copula avoidance") == aiwriting.SKIP
    assert aiwriting.resume_profile_level("Copula Avoidance") == aiwriting.SKIP


# ── (b) RESUME_RULES_PROMPT ──────────────────────────────────────────────────
def test_rules_prompt_is_a_substantial_block_and_names_its_source():
    prompt = aiwriting.RESUME_RULES_PROMPT
    assert isinstance(prompt, str) and len(prompt) > 800
    for token in ("avoid-ai-writing", "3.18.0", "MIT", "Conor Bronsdon"):
        assert token in prompt, token


def test_rules_prompt_states_the_p0_p1_scope():
    prompt = aiwriting.RESUME_RULES_PROMPT
    assert "P0" in prompt and "P1" in prompt
    # P2 polish is detected elsewhere and reported, never auto-fixed this cycle.
    assert "P2" not in prompt


# Each P0/P1 category the prompt has to carry, with an anchor that must appear.
PROMPT_ANCHORS = {
    "tier-1 vocabulary": ("delve", "testament to", "meticulous"),
    "template / slot-fill": ("SLOT-FILL", "blank"),
    "formulaic openings": ("Responsible for", "Tasked with"),
    "hedging": ("potentially", "could potentially"),
    "real/actual inflation": ("real-time", "actual"),
    "bare-noun bullets": ("finite verb",),
    "chatbot artifacts": ("Certainly", "I hope this helps"),
    "vague attribution": ("experts believe", "studies show"),
    "significance inflation": ("watershed moment", "inflation clause"),
}


@pytest.mark.parametrize("category", sorted(PROMPT_ANCHORS))
def test_rules_prompt_covers_every_p0_p1_category(category):
    prompt = aiwriting.RESUME_RULES_PROMPT
    for anchor in PROMPT_ANCHORS[category]:
        assert anchor in prompt, f"{category}: {anchor}"


def test_rules_prompt_pins_the_register_so_the_model_cannot_add_a_subject():
    """Hazard 1, stated to the model directly: without this line the repair call is
    free to "fix" a subjectless fragment into a first-person sentence."""
    prompt = aiwriting.RESUME_RULES_PROMPT
    assert "subjectless" in prompt.lower()
    assert "first person" in prompt.lower()
    assert "Never add a subject" in prompt


def test_rules_prompt_does_not_repeat_the_shared_banned_phrasing():
    """It is appended after compose.BANNED_PHRASING at the call site. Repeating a
    ban there doubles the prompt for no gain and lets the two copies drift."""
    prompt = aiwriting.RESUME_RULES_PROMPT.lower()
    for already_banned in ("robust", "seamless", "comprehensive", "holistic",
                           "leverage", "utilize", "harness", "streamline",
                           "em dash", "participial", "rule of three"):
        assert already_banned not in prompt, already_banned


def test_rules_prompt_obeys_every_rule_it_states():
    """A model copies the punctuation it is shown, so the prompt may not use the
    characters or the sentence shapes it forbids. This is the same scan
    tests/test_prompt_hygiene.py runs across the package, applied here directly so
    the constant is covered from the moment it exists (the package-wide scan only
    sees a constant once it is reachable from an llm.call site)."""
    prompt = aiwriting.RESUME_RULES_PROMPT
    hits = [(label, pattern.search(prompt).group(0))
            for label, pattern in hygiene.BANNED if pattern.search(prompt)]
    assert hits == []


# ── (c) the deterministic arm ────────────────────────────────────────────────
# (positive, plausible near-miss). The near-miss must trip NO pattern at all: a
# false positive here buys a repair call that can damage correct text.
RESUME_BAN_CASES = {
    "tier-1 vocabulary": (
        "Delivered a pivotal migration for the billing service.",
        "Delivered the billing migration two weeks early."),
    "hedge": (
        "Reduced queue latency and may cut it further next quarter.",
        "Built a viewer so reviewers could compare runs side by side."),
    "promotional language": (
        "Grew a thriving community of 300 contributors.",
        "Grew the contributor community from 40 to 300."),
    "significance inflation": (
        "Led a paradigm shift in how the team ships releases.",
        "Led the move to trunk-based development for 12 engineers."),
    "vague attribution": (
        "Rebuilt search because studies show users abandon slow queries.",
        "Rebuilt search after a study of 1,200 sessions showed 4s queries."),
    "formulaic opening": (
        "Responsible for the nightly ETL job and its alerts.",
        "Owned the nightly ETL job and its alerts."),
    "chatbot artifact": (
        "Certainly! Built a scraper that cut per-run cost 65%.",
        CLEAN_BULLET),
}


# A capitalised modal opening a bullet is still a hedge. The no-re.I carve-out that
# protects "in May 2024" drops these three, and the position they sit in is the one
# the pipeline reserves for an action verb, so a hedge there is always a defect.
@pytest.mark.parametrize("bullet", [
    "May have reduced the nightly run time.",
    "Might improve throughput on larger inputs.",
    "Could cut the index rebuild in half.",
])
def test_a_capitalised_modal_opening_a_bullet_is_a_hedge(bullet):
    assert "hedge" in aiwriting.resume_violations(bullet)


# The month keeps its exemption in both positions: mid-bullet by case, and at the
# start by the digit that follows it.
@pytest.mark.parametrize("bullet", [
    "Launched the parser rewrite in May 2024.",
    "May 2024 cohort lead for the intern onboarding track.",
])
def test_the_month_is_not_read_as_a_hedge(bullet):
    assert aiwriting.resume_violations(bullet) == []


def test_ban_cases_cover_every_resume_extra_ban():
    """The table above must not drift out of sync with the ban list."""
    assert sorted(RESUME_BAN_CASES) == sorted(
        name for name, _ in aiwriting.RESUME_EXTRA_BANS)


@pytest.mark.parametrize("name", sorted(RESUME_BAN_CASES))
def test_resume_ban_fires_on_its_positive_example(name):
    positive, _ = RESUME_BAN_CASES[name]
    assert name in aiwriting.resume_violations(positive)


@pytest.mark.parametrize("name", sorted(RESUME_BAN_CASES))
def test_resume_ban_quiet_on_a_near_miss(name):
    _, near_miss = RESUME_BAN_CASES[name]
    assert aiwriting.resume_violations(near_miss) == []


# ── the proof this phase exists for ──────────────────────────────────────────
def test_a_correct_resume_register_bullet_yields_zero_findings():
    """The phase checkpoint. A subjectless fragment opening with a past-tense verb
    is CORRECT résumé grammar, and the upstream skill would flag it twice."""
    assert aiwriting.resume_violations(CLEAN_BULLET) == []


@pytest.mark.parametrize("bullet", [
    "Built a scraper that cut per-run cost 65%.",
    "Cut p99 latency 40% on the ranking service.",
    "Shipped the résumé viewer with 178 tests.",
    "Migrated 12 pipelines to Airflow and removed the cron layer.",
    "Wrote the atomic CSV writer used by every pipeline stage.",
    "Automated the nightly scrape, dropping manual runs to zero.",
])
def test_subjectless_bullets_are_clean(bullet):
    assert aiwriting.resume_violations(bullet) == []


def test_a_first_person_bullet_is_not_pushed_further_toward_first_person():
    """First person is wrong here, and the fix is to remove it. What must never
    happen is the upstream reading, where the ABSENCE of "I think" is the finding.
    So: the profile skips that rule, the prompt says so, and no regex in this arm
    rewards a first-person bullet by staying quiet on the subjectless one."""
    first_person = "I built a scraper that cut per-run cost 65%."
    assert aiwriting.RESUME_PROFILE[
        "missing first-person perspective"].level == aiwriting.SKIP
    # Neither form is flagged BY THIS ARM for its use or non-use of a subject.
    assert aiwriting.resume_violations(first_person) == []
    assert aiwriting.resume_violations(CLEAN_BULLET) == []
    assert "first person" in aiwriting.RESUME_RULES_PROMPT.lower()


# ── the technical carve-out ──────────────────────────────────────────────────
@pytest.mark.parametrize("bullet", [
    # Skill words with a real engineering sense a bullet could legitimately carry.
    # These stay in the prompt arm, where the model can read context.
    "Configured the Keycloak realm and rotated its signing keys.",       # realm
    "Migrated the service to an event-driven paradigm.",                # paradigm
    "Mapped the competitive landscape for the pricing team.",           # landscape
    "Shipped BLE beacon firmware for 400 devices.",                     # beacon
    "Documented the team's best practices for code review.",            # best practices
    "Turned the survey into actionable next steps.",                    # actionable
    "Built a real-time dashboard covering 12 pipelines.",               # real / real-time
    "Reviewed the actual runtime of every query in the report.",        # actual
    "Generalized the loader to handle three file formats.",             # generally
    "Launched the parser rewrite in May 2024.",                         # may (the month)
    "Cut p99 latency 40% so the cron could finish inside its window.",  # could (purpose)
])
def test_context_sensitive_words_stay_out_of_the_regex_arm(bullet):
    assert aiwriting.resume_violations(bullet) == []


# ── shape and non-duplication ────────────────────────────────────────────────
def test_resume_violations_returns_names_and_is_empty_for_clean_text():
    assert aiwriting.resume_violations(CLEAN_BULLET) == []
    # The hedge sits in the MAIN clause here on purpose. Put it behind a
    # subordinator ("... that experts believe may help") and the pattern declines,
    # which is the purpose-clause carve-out working, and no miss.
    names = aiwriting.resume_violations(
        "Responsible for a pivotal rollout and may extend it; studies show it helps.")
    assert set(names) >= {"formulaic opening", "tier-1 vocabulary",
                          "vague attribution", "hedge"}
    assert all(isinstance(n, str) for n in names)
    assert aiwriting.resume_violations(
        "Shipped a rollout that experts believe may help.") == ["vague attribution"]


def test_resume_extra_bans_have_the_same_shape_as_the_letter_arm():
    for entry in aiwriting.RESUME_EXTRA_BANS:
        assert isinstance(entry, tuple) and len(entry) == 2
        name, pattern = entry
        assert isinstance(name, str) and isinstance(pattern, re.Pattern)


def test_resume_bans_do_not_duplicate_the_deterministic_bullet_gate():
    """compose._STYLE_BANS is the always-on backstop underneath this arm. Naming the
    same finding twice would double-count it in any strict-improvement check."""
    compose_names = {name for name, _ in compose._STYLE_BANS}
    resume_names = {name for name, _ in aiwriting.RESUME_EXTRA_BANS}
    assert compose_names & resume_names == set()


def test_a_bullet_that_only_trips_compose_is_left_to_compose():
    dirty = "Built a robust pipeline, enabling faster nightly runs."
    assert compose.style_violations(dirty)          # compose owns this one
    assert aiwriting.resume_violations(dirty) == []


def test_the_tier1_regex_is_shared_with_the_letter_arm_not_copied():
    """One compiled object, so the vocabulary cannot drift between the two arms."""
    letter = dict(aiwriting.EXTRA_BANS)["tier-1 vocabulary"]
    resume = dict(aiwriting.RESUME_EXTRA_BANS)["tier-1 vocabulary"]
    assert letter is resume
