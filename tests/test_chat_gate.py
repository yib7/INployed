"""The chat's post-answer AI-writing gate, `chat._prose_gate`.

`chat.ask` strips em dashes unconditionally and, on an answer of
`PROSE_WORD_FLOOR` words or more, checks it against the same two deterministic
checks the résumé and letter arms use (`compose.style_violations`,
`aiwriting.violations`) and buys one flash repair call when either fires. The
repair is committed only on strict improvement, exactly like
`compose.enforce_style` and `coverletter.enforce_body_style` -- a bad repair
must never make an answer worse.

Kept in its own file (split out of test_chat_context.py) so the gate's cases
-- the word floor, the repair-commit rule, the em-dash backstop -- read as one
group. No real LLM ever runs: `chat.call` is monkeypatched throughout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import chat, config  # noqa: E402

DASH = chr(0x2014)


def _words(n: int, trigger: str = "") -> str:
    """`n` plain words, with the LAST one swapped for `trigger` when given."""
    words = ["plain"] * n
    if trigger:
        words[-1] = trigger
    return " ".join(words)


def _fake_call(monkeypatch, responses):
    """`chat.call` returning `responses` in order, one per invocation. Records
    every (system, user, tier, kwargs) tuple onto the returned list."""
    calls = []
    it = iter(responses)

    def fake(system, user, tier, **kw):
        calls.append((system, user, tier, kw))
        return next(it)

    monkeypatch.setattr(chat, "call", fake)
    return calls


# ── the word floor ───────────────────────────────────────────────────────────
def test_fifty_nine_words_with_a_violation_skips_the_gate(monkeypatch):
    text = _words(59, "delve")
    calls = _fake_call(monkeypatch, ["should never be used"])
    assert chat._prose_gate(text) == text
    assert calls == []


def test_sixty_words_with_a_violation_triggers_exactly_one_repair_call(monkeypatch):
    text = _words(60, "delve")
    fixed = _words(60)
    calls = _fake_call(monkeypatch, [fixed])
    assert chat._prose_gate(text) == fixed
    assert len(calls) == 1


def test_sixty_words_with_no_violation_never_calls_out(monkeypatch):
    text = _words(60)
    calls = _fake_call(monkeypatch, ["should never be used"])
    assert chat._prose_gate(text) == text
    assert calls == []


# ── the repair-commit rule ───────────────────────────────────────────────────
def test_a_repair_that_still_violates_is_rejected(monkeypatch):
    """A mocked repair call that returns text with the SAME banned pattern
    buys nothing: the original answer stands."""
    text = _words(60, "delve")
    still_bad = _words(60, "delve")
    calls = _fake_call(monkeypatch, [still_bad])
    assert chat._prose_gate(text) == text
    assert len(calls) == 1


def test_a_repair_that_removes_the_pattern_is_committed(monkeypatch):
    text = _words(60, "delve")
    fixed = _words(60)
    _fake_call(monkeypatch, [fixed])
    assert chat._prose_gate(text) == fixed


def test_a_repair_with_strictly_fewer_findings_still_commits(monkeypatch):
    """Two findings in, one out: a partial fix still counts as improvement."""
    words = ["plain"] * 58 + ["delve", "leverage"]
    text = " ".join(words)
    partial = " ".join(["plain"] * 59 + ["leverage"])   # "delve" gone, "leverage" stays
    _fake_call(monkeypatch, [partial])
    assert chat._prose_gate(text) == partial


def test_a_blank_repair_reply_is_rejected(monkeypatch):
    text = _words(60, "delve")
    _fake_call(monkeypatch, [""])
    assert chat._prose_gate(text) == text


def test_a_whitespace_only_repair_reply_is_rejected(monkeypatch):
    """`"   \\n  ".strip()` is also blank; it must not be treated as a real fix."""
    text = _words(60, "delve")
    _fake_call(monkeypatch, ["   \n  "])
    assert chat._prose_gate(text) == text


def test_a_failed_repair_call_leaves_the_original_answer(monkeypatch):
    text = _words(60, "delve")

    def boom(system, user, tier, **kw):
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr(chat, "call", boom)
    assert chat._prose_gate(text) == text


def test_the_repair_call_runs_on_the_flash_tier_with_a_low_temperature(monkeypatch):
    text = _words(60, "delve")
    fixed = _words(60)
    calls = _fake_call(monkeypatch, [fixed])
    chat._prose_gate(text)
    _system, _user, tier, kw = calls[0]
    assert tier == config.TIER_FLASH
    assert kw.get("temperature") == 0.2


def test_the_repair_prompt_names_the_findings(monkeypatch):
    text = _words(60, "delve")
    fixed = _words(60)
    calls = _fake_call(monkeypatch, [fixed])
    chat._prose_gate(text)
    _system, user, _tier, _kw = calls[0]
    assert "tier-1 vocabulary" in user


# ── the em-dash backstop on the committed repair ─────────────────────────────
def test_em_dashes_in_a_committed_repair_are_also_stripped(monkeypatch):
    """Two findings in the original (tier-1 word + buzzword verb); the mocked
    repair clears both but leaves a stray em dash of its own -- one finding is
    still strictly fewer than two, so it commits, and the dash must not survive
    that commit."""
    text = " ".join(["plain"] * 58 + ["delve", "leverage"])
    fixed = " ".join(["plain"] * 59) + " done" + DASH + "finished."
    calls = _fake_call(monkeypatch, [fixed])
    result = chat._prose_gate(text)
    assert DASH not in result
    assert len(calls) == 1


# ── ask() wires _prose_gate in, with the unconditional em-dash strip first ───
def test_ask_strips_em_dashes_even_under_the_word_floor(monkeypatch):
    def fake(system, user, tier, **kw):
        return "two words" + DASH + "done."

    monkeypatch.setattr(chat, "call", fake)
    assert DASH not in chat.ask("ctx", [], "q")
