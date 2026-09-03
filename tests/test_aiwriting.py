"""The opt-in avoid-AI-writing pass on the cover-letter body (default OFF).

`resume_tailor.aiwriting` vendors a bounded extract of the avoid-ai-writing
skill (v3.18.0, MIT, Conor Bronsdon): a prompt block for the judgment calls and
a small deterministic ban list for the patterns that are ALWAYS slop. It extends
the existing two-arm gate rather than replacing it, so the hard requirement is
that with the toggle OFF nothing changes at all -- the letter prompts stay
byte-identical to what shipped before the toggle existed.

No real LLM ever runs: compose.call (the transport coverletter uses) is
monkeypatched, and the toggle is driven through the real config accessor rather
than a stub so the env > config.json > False precedence is exercised for real.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import aiwriting, compose, config, coverletter  # noqa: E402


BULLETS = {"a1": "Shipped the viewer with 178 tests",
           "a2": "Cut per-run cost by 65%"}

# The letter-owned half of TODAY's system prompts, captured verbatim from the
# code that shipped before this toggle existed. The shared tail is referenced
# (not copied) so an intentional edit to compose.BANNED_PHRASING -- which the
# résumé arm shares -- doesn't fail here with a misleading message.
#
# What these two pins protect is the DELTA (the rules block is appended and
# nothing else moves), not the wording of the head itself. A deliberate reword of
# the cover-letter prompt re-pins the head here; a head that drifts without anyone
# meaning to is the failure this catches. Cycle 10 re-pinned two em dashes out
# (see tests/test_prompt_hygiene.py for why a prompt may not use them).
_TODAYS_GENERATE_HEAD = (
    'Write a concise, genuine cover-letter body (3 short paragraphs) for an '
    'early-career candidate. Use ONLY facts present in the provided resume bullets and '
    "basics; never invent experience, numbers, or interest you can't support. No "
    'salutation and no sign-off (the template adds them). Plain text, paragraphs '
    'separated by a blank line. Warm but professional; write like a person, in plain '
    'declarative sentences, no clichés. Show genuine but MEASURED interest: never gush '
    'or over-sell: no exclamation-point excitement, no '
    "'thrilled/ecstatic/passionate/love' inflation, no empty superlatives; that "
    'over-eager tone reads as AI-written. Use a confident, professional tone. Use the '
    'correct tense for education, based on the EDUCATION line: if the candidate has '
    "already graduated, NEVER say they are 'completing' or 'finishing' their studies; "
    "refer to the degree as completed. Do NOT open with boilerplate ('I am writing to "
    "express my interest...', 'I am writing to apply for...'): the FIRST sentence must "
    'lead with something specific about the candidate or the company. Never use the '
    'same metric or number twice in the letter.\nBANNED PHRASING (using any of these is '
    'wrong): '
)

_TODAYS_REFINE_HEAD = (
    'You are an editor doing a final polish pass on a cover-letter body. Improve '
    'cohesion and flow so it reads as one connected argument, not stitched-together '
    'sentences. Stay grounded: use ONLY facts already in the draft and the resume '
    "bullets below; never add a company, number, skill, or claim that isn't supported, "
    'and cut anything the draft invented. Keep the meaning and roughly the same length; '
    'no salutation and no sign-off. Show genuine but MEASURED interest: do NOT be '
    'over-the-top or gushing. No exclamation-point enthusiasm, no '
    "'thrilled/ecstatic/passionate/love' inflation, no empty superlatives; that "
    'over-eager tone reads as AI-written. Use a confident, professional tone.\nBANNED '
    'PHRASING (do not introduce any of these): '
)


@pytest.fixture
def toggle(monkeypatch):
    """Drive the real accessor: env cleared, config.json stubbed to the value."""
    def _set(on: bool):
        monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
        monkeypatch.setattr(config, "_config_json",
                            lambda: {"cover_letter_avoid_ai_writing": on})
    return _set


def _fake_master(monkeypatch):
    monkeypatch.setattr(coverletter.assets, "load_master",
                        lambda: {"basics": {"name": "Test User", "location": "NYC"}})


# ── attribution ───────────────────────────────────────────────────────────────
def test_module_docstring_credits_the_skill():
    doc = aiwriting.__doc__ or ""
    for token in ("avoid-ai-writing", "3.18.0", "MIT", "Conor Bronsdon", "SKILL.md"):
        assert token in doc, token


# ── EXTRA_BANS: each pattern fires on slop, stays quiet on a near-miss ────────
# (positive, plausible near-miss). The near-miss must trip NO pattern at all: a
# false positive here buys a repair call that can damage correct text.
BAN_CASES = {
    "tier-1 vocabulary": (
        "I want to delve into the platform work your team published.",
        "I delivered an ordering service and boosted core test coverage."),
    "chatbot artifact": (
        "I hope this helps as you review candidates.",
        "I hope to help your team ship the pipeline sooner."),
    "let's opener": (
        "Let's start with the throughput problem I solved.",
        "The scheduler lets the team retry failed runs."),
    "era framing": (
        "In today's market, data teams move quickly.",
        "Today I finished the parser rewrite."),
    "confidence calibration": (
        "It's worth noting that I shipped it two weeks early.",
        "Their mission is worth my full attention, and I noted why."),
    "generic conclusion": (
        "In conclusion, I would bring the same care to Acme.",
        "I reached that conclusion after two experiments."),
}


def test_ban_cases_cover_every_extra_ban():
    """The table above must not drift out of sync with the ban list."""
    assert sorted(BAN_CASES) == sorted(name for name, _ in aiwriting.EXTRA_BANS)


@pytest.mark.parametrize("name", sorted(BAN_CASES))
def test_extra_ban_fires_on_its_positive_example(name):
    positive, _ = BAN_CASES[name]
    assert name in aiwriting.violations(positive)


@pytest.mark.parametrize("name", sorted(BAN_CASES))
def test_extra_ban_quiet_on_a_near_miss(name):
    _, near_miss = BAN_CASES[name]
    assert aiwriting.violations(near_miss) == []


@pytest.mark.parametrize("word", [
    "delve", "delving", "testament to", "pivotal", "meticulously", "nestled",
    "bustling", "intricacies", "ever-evolving", "daunting", "impactful",
    "learnings", "thought leadership", "at its core", "synergies", "interplay",
    "in order to", "due to the fact that", "boasts", "showcasing",
    "watershed moment",
])
def test_tier1_vocabulary_words_are_all_banned(word):
    assert "tier-1 vocabulary" in aiwriting.violations(f"The team {word} the roadmap.")


@pytest.mark.parametrize("text", [
    # Words the skill lists but that carry a load-bearing sense in a job letter:
    # a false positive on any of these would rewrite a correct, grounded fact.
    "I configured the Keycloak realm and rotated its signing keys.",       # realm
    "I moved the service to an event-driven paradigm.",                    # paradigm
    "I mapped the competitive landscape for the pricing team.",            # landscape
    "I shipped the BLE beacon firmware for 400 devices.",                  # beacon
    "I followed the team's best practices for code review.",               # best practices
    "I turned the survey into actionable next steps.",                     # actionable
    "I have a genuine interest in your compiler work.",                    # genuine
    "I want to embrace the ownership this role expects.",                  # embrace
    "The pipeline underscores every filename with a run id.",              # underscores
])
def test_context_sensitive_words_stay_out_of_the_regex_arm(text):
    assert aiwriting.violations(text) == []


def test_violations_returns_names_and_is_empty_for_clean_text():
    assert aiwriting.violations("I cut per-run cost by 65% and shipped the viewer.") == []
    names = aiwriting.violations("In conclusion, let me delve into it. I hope this helps.")
    assert set(names) >= {"generic conclusion", "tier-1 vocabulary", "chatbot artifact"}
    assert all(isinstance(n, str) for n in names)


def test_rules_prompt_covers_the_judgment_calls():
    rules = aiwriting.RULES_PROMPT
    assert isinstance(rules, str) and len(rules) > 400
    # The context-sensitive patterns live here, not in the regex arm.
    for anchor in ("rule of three", "rhetorical", "hedg", "sentence length"):
        assert anchor.lower() in rules.lower(), anchor


# ── the accessor: env > config.json > False ───────────────────────────────────
def test_avoid_ai_writing_defaults_to_false(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.avoid_ai_writing_enabled() is False


def test_avoid_ai_writing_config_on(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"cover_letter_avoid_ai_writing": True})
    assert config.avoid_ai_writing_enabled() is True


def test_avoid_ai_writing_config_off(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"cover_letter_avoid_ai_writing": False})
    assert config.avoid_ai_writing_enabled() is False


def test_avoid_ai_writing_env_on(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_AVOID_AI_WRITING", "1")
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.avoid_ai_writing_enabled() is True


def test_avoid_ai_writing_env_beats_config_on(monkeypatch):
    """env wins BOTH ways, including turning a config-enabled pass back off."""
    monkeypatch.setenv("RESUME_TAILOR_AVOID_AI_WRITING", "off")
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"cover_letter_avoid_ai_writing": True})
    assert config.avoid_ai_writing_enabled() is False


def test_avoid_ai_writing_blank_env_falls_through_to_config(monkeypatch):
    monkeypatch.setenv("RESUME_TAILOR_AVOID_AI_WRITING", "  ")
    monkeypatch.setattr(config, "_config_json",
                        lambda: {"cover_letter_avoid_ai_writing": True})
    assert config.avoid_ai_writing_enabled() is True


def test_settings_schema_exposes_the_toggle_defaulting_off():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
    import settings  # noqa: PLC0415 - imported here so the path insert above applies

    field = next(f for f in settings.SETTINGS_SCHEMA
                 if f.key == "cover_letter_avoid_ai_writing")
    assert (field.type, field.default, field.section, field.target) == (
        "bool", False, "Resume", "config")


def test_the_attribution_survives_the_help_trim():
    """The help string used to carry the credit, and P8 cut it from ~700 chars to
    two sentences to sit under the schema's 350-char cap.

    The attribution is not optional, so this asserts it MOVED rather than that it
    is gone: the full citation (name, version, licence, source path) lives in
    `docs/CREDITS.md`, the user-facing description of what the pass catches lives
    in `docs/USER_GUIDE.md`, and the field help points at the guide so someone
    reading the checkbox can still get there.
    """
    docs = Path(__file__).resolve().parents[1] / "docs"
    credits = (docs / "CREDITS.md").read_text(encoding="utf-8")
    guide = (docs / "USER_GUIDE.md").read_text(encoding="utf-8")
    for text in (credits, guide):
        assert "Bronsdon" in text
        assert "avoid-ai-writing" in text
        assert "MIT" in text

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
    import settings  # noqa: PLC0415

    field = next(f for f in settings.SETTINGS_SCHEMA
                 if f.key == "cover_letter_avoid_ai_writing")
    assert "USER_GUIDE.md" in field.help


# ── prompt wiring: additive when ON, byte-identical when OFF ──────────────────
def _capture_generate_system(monkeypatch):
    _fake_master(monkeypatch)
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
    return seen["system"]


def test_generate_prompt_is_byte_identical_when_off(monkeypatch, toggle):
    toggle(False)
    assert _capture_generate_system(monkeypatch) == (
        _TODAYS_GENERATE_HEAD + compose.BANNED_PHRASING)


def test_generate_prompt_appends_rules_when_on(monkeypatch, toggle):
    toggle(True)
    assert _capture_generate_system(monkeypatch) == (
        _TODAYS_GENERATE_HEAD + compose.BANNED_PHRASING + "\n" + aiwriting.RULES_PROMPT)


def _capture_refine_system(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.refine_body("jd", "Engineer", "Acme", "A draft body.", BULLETS)
    return seen["system"]


def test_refine_prompt_is_byte_identical_when_off(monkeypatch, toggle):
    toggle(False)
    assert _capture_refine_system(monkeypatch) == (
        _TODAYS_REFINE_HEAD + compose.BANNED_PHRASING)


def test_refine_prompt_appends_rules_when_on(monkeypatch, toggle):
    toggle(True)
    assert _capture_refine_system(monkeypatch) == (
        _TODAYS_REFINE_HEAD + compose.BANNED_PHRASING + "\n" + aiwriting.RULES_PROMPT)


# ── the deterministic gate ────────────────────────────────────────────────────
_AIWRITING_ONLY = ("I want to delve into your compiler work.\n\n"
                   "In conclusion, I shipped the viewer with 178 tests.")


def test_gate_ignores_aiwriting_patterns_when_off(monkeypatch, toggle):
    toggle(False)
    calls = []
    monkeypatch.setattr(compose, "call", lambda *a, **k: calls.append(1) or "")
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", _AIWRITING_ONLY, BULLETS)
    assert out == _AIWRITING_ONLY
    assert calls == []  # clean under compose's bans alone -> no LLM call at all


def test_gate_repairs_aiwriting_patterns_when_on(monkeypatch, toggle):
    toggle(True)
    repaired = "I read your compiler work closely.\n\nI shipped the viewer with 178 tests."
    monkeypatch.setattr(compose, "call", lambda *a, **k: repaired)
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", _AIWRITING_ONLY, BULLETS)
    assert out == repaired


def test_gate_rejects_a_repair_that_does_not_strictly_improve(monkeypatch, toggle):
    """The strict-improvement rule must count BOTH ban sets, not just compose's."""
    toggle(True)
    # The "repair" swaps one tier-1 word for another: same violation count -> reject.
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: "In conclusion, this is a pivotal role for me.")
    out = coverletter.enforce_body_style("jd", "Engineer", "Acme", _AIWRITING_ONLY, BULLETS)
    assert out == _AIWRITING_ONLY


def test_gate_repair_prompt_carries_the_rules_when_on(monkeypatch, toggle):
    toggle(True)
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "I read your compiler work.\n\nI shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.enforce_body_style("jd", "Engineer", "Acme", _AIWRITING_ONLY, BULLETS)
    assert aiwriting.RULES_PROMPT in seen["system"]
    assert "tier-1 vocabulary" in seen["user"]  # the named violations reach the repair


def test_grounding_gate_still_runs_last_with_the_toggle_on(monkeypatch, toggle):
    """The style pass must not become the last word: an unsupported fact still fails."""
    toggle(True)
    _fake_master(monkeypatch)
    monkeypatch.setattr(coverletter, "refine_body",
                        lambda jd, jt, co, body, bullets, **k: body)
    monkeypatch.setattr(compose, "call",
                        lambda *a, **k: "I led the Zorblatt migration for 12 teams.")
    with pytest.raises(coverletter.LLMError, match="ungrounded"):
        coverletter.generate_body("jd", "Engineer", "Acme", BULLETS)
