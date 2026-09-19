"""SP3 (cycle 15): the cover letter as narrative.

With the seed and the background in place (tests/test_coverletter_inputs.py),
this pins what the letter does with them:

  * the generation system prompt is a narrative brief (one through-line, open
    on the link to the role, one or two experiences as prose, close on what
    comes next, visibly different paragraph lengths, never a copied bullet);
  * the refine pass is the humanizer (rhythm, paragraph variance, a bullet with
    a subject bolted on retold as narrative, measured tone), still grounded;
  * both, and the repair prompts, always carry `aiwriting.RULES_PROMPT`,
    whatever the Settings toggle says;
  * the two structural detectors, `aiwriting.bullet_echo` and
    `aiwriting.uniform_rhythm`, join the deterministic gate whatever the toggle
    says, and the repair prompt explains them.

No real LLM ever runs: compose.call (the transport coverletter uses) is
monkeypatched everywhere, and every master read is a synthetic dict.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import aiwriting, assets, common, compose, config, coverletter  # noqa: E402


BULLETS = {"a1": "Shipped the viewer with 178 tests",
           "a2": "Cut per-run cost by 65%"}


def _fake_master(monkeypatch):
    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"name": "Test User", "location": "NYC"}})


@pytest.fixture
def toggle(monkeypatch):
    def _set(on: bool):
        monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
        monkeypatch.setattr(config, "_config_json",
                            lambda: {"cover_letter_avoid_ai_writing": on})
    return _set


# ── bullet_echo ───────────────────────────────────────────────────────────────
ECHO_BULLET = "Rebuilt the nightly data-ingestion pipeline with a batched async fetcher"


def test_bullet_echo_flags_a_seven_word_shingle():
    body = ("At Example Corp I rebuilt the nightly data-ingestion pipeline with a "
            "batched approach.")
    assert aiwriting.bullet_echo(body, {"a1": ECHO_BULLET}) == ["bullet echo"]


def test_bullet_echo_passes_a_paraphrase():
    body = ("The nightly ingestion job was the bottleneck at Example Corp, so I "
            "batched its fetcher and the run finished in ninety minutes.")
    assert aiwriting.bullet_echo(body, {"a1": ECHO_BULLET}) == []


def test_bullet_echo_folds_case_and_punctuation():
    body = "I REBUILT the nightly, data ingestion pipeline (with a batched) async fetcher!"
    assert aiwriting.bullet_echo(body, [ECHO_BULLET]) == ["bullet echo"]


def test_bullet_echo_needs_seven_words_in_a_row():
    # six shared words, then the sentence turns
    body = "I rebuilt the nightly data-ingestion pipeline for the billing team."
    assert aiwriting.bullet_echo(body, [ECHO_BULLET]) == []
    # a bullet shorter than the shingle can never echo
    assert aiwriting.bullet_echo("Shipped the viewer with 178 tests.",
                                 ["Shipped the viewer with 178 tests"]) == []


def test_bullet_echo_is_quiet_on_blank_input():
    assert aiwriting.bullet_echo("", [ECHO_BULLET]) == []
    assert aiwriting.bullet_echo("A body.", []) == []


# ── uniform_rhythm ────────────────────────────────────────────────────────────
_UNIFORM = ("I built the parser for the billing team last spring. "
            "I wrote the tests for the parser over two weeks. "
            "I moved the service onto the new queue in June. "
            "I cut the nightly run from six hours to one. "
            "I read your posting for the platform role twice. "
            "I want to bring the same habits to your team.")

_VARIED = ("Your posting asks for someone who has shipped a pipeline under load, "
           "and that is the work I did at Example Corp for most of a summer. "
           "The nightly job took six hours. "
           "It failed most weeks. "
           "I rebuilt the fetcher so it batched its calls, and the same job "
           "finished in ninety minutes with the cloud bill four hundred dollars lighter. "
           "Nothing about it was glamorous. "
           "That is the kind of problem I want next.")


def test_uniform_rhythm_flags_six_same_length_sentences():
    assert len(common.split_sentences(_UNIFORM)) == 6
    assert aiwriting.uniform_rhythm(_UNIFORM) == ["uniform rhythm"]


def test_uniform_rhythm_passes_mixed_sentence_lengths():
    # Six sentences, so the check reaches the coefficient-of-variation path
    # and passes on the merits, never on the too-few-sentences gate.
    counts = [len(s.split()) for s in common.split_sentences(_VARIED)]
    assert len(counts) >= aiwriting.RHYTHM_MIN_SENTENCES
    assert min(counts) < 8 < 20 < max(counts)
    assert aiwriting.uniform_rhythm(_VARIED) == []


def test_sentence_splitter_keeps_abbreviations_whole():
    """One splitter for the rhythm detector and the grounding tracer: "B.S. "
    and "U.S. " never end a sentence, so a four-sentence body counts as four."""
    body = ("I finished my B.S. in computer science last May. "
            "The U.S. office ran the nightly job I rebuilt. "
            "It took six hours. "
            "Now it takes ninety minutes.")
    sentences = common.split_sentences(body)
    assert len(sentences) == 4
    assert sentences[0].endswith("last May.") and "B.S." in sentences[0]
    assert sentences[1].startswith("The U.S. office")
    # the rhythm detector sees the same four (under its six-sentence gate)
    assert aiwriting.uniform_rhythm(body) == []
    # a newline is always a boundary, punctuation or not
    assert common.split_sentences("One line\nanother") == ["One line", "another"]
    assert common.split_sentences("") == []


def test_uniform_rhythm_needs_six_sentences():
    five = ". ".join(_UNIFORM.split(". ")[:5]) + "."
    assert aiwriting.uniform_rhythm(five) == []
    assert aiwriting.uniform_rhythm("") == []


def test_uniform_rhythm_flags_paragraphs_of_one_length():
    # Sentence lengths vary enough (CV over 0.30) but the three paragraphs all
    # land within 15% of the mean paragraph length.
    para = ("The job took six hours. I rebuilt the fetcher so it batched every call "
            "and the same job finished in ninety minutes. It stuck.")
    body = "\n\n".join([para, para, para])
    assert aiwriting.uniform_rhythm(body) == ["uniform rhythm"]


def test_uniform_rhythm_passes_paragraphs_of_different_lengths():
    long = ("The job took six hours. I rebuilt the fetcher so it batched every call "
            "and the same job finished in ninety minutes with the bill lighter. "
            "It stuck, and the team kept it.")
    short = "That is the work I want next."
    body = "\n\n".join([long, long, short])
    assert aiwriting.uniform_rhythm(body) == []


# ── the gate: both detectors run whatever the toggle says ─────────────────────
@pytest.mark.parametrize("on", [True, False])
def test_body_violations_carry_echo_and_rhythm_regardless_of_toggle(toggle, on):
    toggle(on)
    body = _UNIFORM + "\n\nI rebuilt the nightly data-ingestion pipeline with a batched fetcher."
    names = coverletter._body_violations(body, {"a1": ECHO_BULLET})
    assert "bullet echo" in names and "uniform rhythm" in names


def test_gate_repair_prompt_explains_the_structural_findings(monkeypatch):
    body = "I rebuilt the nightly data-ingestion pipeline with a batched async fetcher."
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "The nightly job was the bottleneck, so I batched its fetcher."

    monkeypatch.setattr(compose, "call", fake_call)
    out = coverletter.enforce_body_style("Engineer", "Acme", body, {"a1": ECHO_BULLET},
                                         background="- Example Corp\n    - notes")
    assert out == "The nightly job was the bottleneck, so I batched its fetcher."
    assert "bullet echo" in seen["user"]
    assert "seven" in seen["system"].lower() or "7" in seen["system"]
    assert "narrative" in seen["system"].lower()
    assert "BACKGROUND" in seen["user"] and "- notes" in seen["user"]
    assert aiwriting.RULES_PROMPT in seen["system"]


def test_gate_repair_prompt_explains_uniform_rhythm(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return _VARIED

    monkeypatch.setattr(compose, "call", fake_call)
    out = coverletter.enforce_body_style("Engineer", "Acme", _UNIFORM, BULLETS)
    assert out == _VARIED
    assert "uniform rhythm" in seen["user"]
    assert "clearly shorter" in seen["system"]
    assert "bullet echo" not in seen["system"]      # only the findings present are explained


def test_gate_rejects_a_repair_that_still_echoes(monkeypatch):
    body = _UNIFORM
    # The repair fixes the rhythm and copies a bullet: same count, so rejected.
    monkeypatch.setattr(compose, "call", lambda *a, **k: _VARIED + " I " + ECHO_BULLET.lower() + ".")
    out = coverletter.enforce_body_style("Engineer", "Acme", body, {"a1": ECHO_BULLET})
    assert out == body


# ── the generation prompt ─────────────────────────────────────────────────────
def _capture_generate(monkeypatch, **kwargs):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        seen.setdefault("user", user)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS, **kwargs)
    return seen


def test_generation_system_prompt_is_the_narrative_brief(monkeypatch):
    _fake_master(monkeypatch)
    system = _capture_generate(monkeypatch)["system"]
    low = system.lower()
    for anchor in ("through-line", "one thing about this role", "problem",
                   "what came of it", "what they want to do next", "three or four paragraphs",
                   "different lengths", "mixed length", "never copy a bullet",
                   "travels with"):
        assert anchor in low, anchor
    # what creativity is allowed, and what never is
    assert "framing" in low and "ordering" in low
    assert "new employer, number, tool, date, school or credential" in low
    # the rules that carried over
    assert "NEVER say" in system and "completing" in system and "completed" in system
    assert "I am writing to express my interest" in system
    assert "FIRST sentence" in system
    assert "same metric or number twice" in system
    assert "MEASURED" in system
    assert compose.BANNED_PHRASING in system
    assert "3 short paragraphs" not in system


@pytest.mark.parametrize("on", [True, False])
def test_generation_prompt_always_carries_the_rules(monkeypatch, toggle, on):
    toggle(on)
    _fake_master(monkeypatch)
    system = _capture_generate(monkeypatch)["system"]
    assert system.endswith("\n" + aiwriting.RULES_PROMPT)


def test_generation_prompt_carries_no_banned_styling(monkeypatch):
    """The brief must not model what it forbids (the model copies what it sees)."""
    _fake_master(monkeypatch)
    seen = _capture_generate(monkeypatch, background="- notes", seed="my seed")
    head = seen["system"].split("BANNED PHRASING")[0]
    assert compose.style_violations(head) == []
    assert aiwriting.violations(head) == []
    assert compose.style_violations(seen["user"]) == []


# ── the humanizer pass ────────────────────────────────────────────────────────
def _capture_refine(monkeypatch, **kwargs):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "ok"

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.refine_body("Engineer", "Acme", "the draft body", BULLETS, **kwargs)
    return seen


def test_refine_is_the_humanizer(monkeypatch):
    seen = _capture_refine(monkeypatch, tone="concise", background="- Example Corp\n    - notes")
    low = seen["system"].lower()
    for anchor in ("one connected argument", "sentence length", "clearly shorter",
                   "bullet with a subject bolted on", "narrative", "measured"):
        assert anchor in low, anchor
    # grounding stays: draft + bullets + background, never anything new
    assert "ONLY facts" in seen["system"]
    assert "never add" in seen["system"] and "invent" in low
    assert "BACKGROUND" in seen["user"] and "- notes" in seen["user"]
    assert "the draft body" in seen["user"]
    assert "Shipped the viewer with 178 tests" in seen["user"]
    assert coverletter.tone_directive("concise") in seen["system"]
    assert compose.BANNED_PHRASING in seen["system"]


@pytest.mark.parametrize("on", [True, False])
def test_refine_prompt_always_carries_the_rules(monkeypatch, toggle, on):
    toggle(on)
    system = _capture_refine(monkeypatch)["system"]
    assert system.endswith("\n" + aiwriting.RULES_PROMPT)


def test_humanizer_prompt_carries_no_banned_styling(monkeypatch):
    seen = _capture_refine(monkeypatch, background="- notes")
    head = seen["system"].split("BANNED PHRASING")[0]
    assert compose.style_violations(head) == []
    assert aiwriting.violations(head) == []


@pytest.mark.parametrize("on", [True, False])
def test_grounding_repair_always_carries_the_rules(monkeypatch, toggle, on):
    toggle(on)
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"] = system
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter._repair_ungrounded_body("Engineer", "Acme", "I led the Zorblatt migration.",
                                        BULLETS, ["Zorblatt"], "professional")
    assert seen["system"].endswith("\n" + aiwriting.RULES_PROMPT)


# ── the default flip ──────────────────────────────────────────────────────────
def test_avoid_ai_writing_now_defaults_on(monkeypatch):
    monkeypatch.delenv("RESUME_TAILOR_AVOID_AI_WRITING", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    assert config.avoid_ai_writing_enabled() is True
    monkeypatch.setattr(config, "_config_json", lambda: {"cover_letter_avoid_ai_writing": False})
    assert config.avoid_ai_writing_enabled() is False


def test_settings_schema_default_is_true_and_help_says_what_the_toggle_now_does():
    import settings  # noqa: PLC0415

    field = next(f for f in settings.SETTINGS_SCHEMA
                 if f.key == "cover_letter_avoid_ai_writing")
    assert field.default is True
    assert "taste call" not in field.help
    assert "Off by default" not in field.help
    assert "USER_GUIDE.md" in field.help
