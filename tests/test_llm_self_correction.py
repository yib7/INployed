"""A JSON answer the model corrects in its own output (`llm._extract_json`).

On the Claude CLI lane at low effort the model has no hidden reasoning, so it
checks its work in the reply itself. On the AI-writing sweep's repair calls it
often wrote a complete answer carrying a key nobody asked for ("max_chars",
"max_chars_check", "_", "note"), then a line of prose ("Wait, correcting...") and
a second, corrected answer. Every such reply failed to parse: the fallback took
the span from the first "{" to the last "}", which held both answers and the prose
between them. Each failure cost a full retry of the call, and a second failure
dropped the entry's repair. The replies below are copied from those runs, with
the bullet text shortened.

The answer is the LAST complete JSON value that opens a line, which is the one
the model settled on. Nothing here reaches a model.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import claude_cli  # noqa: E402
from resume_tailor import llm, sweep  # noqa: E402

# A complete first answer with an echoed key, the model's note, and the corrected
# answer (the dominant shape: 20 of the 22 failed replies).
_ECHOED_KEY = (
    '{"bullets":[{"gkey":"nrhh_selection","text":"Selected for a national honorary '
    'for service and community leadership.","max_chars":124},{"gkey":"nrhh_events",'
    '"text":"Helped host staff-appreciation banquets.","max_chars":124}]}\n\n'
    'Correction: the output must use only the "gkey" and "text" keys. Final:\n\n'
    '{"bullets":[{"gkey":"nrhh_selection","text":"Selected for a national honorary '
    'for service and community leadership."},{"gkey":"nrhh_events","text":"Helped '
    'host staff-appreciation banquets."}]}'
)
_ECHOED_FINAL = {"bullets": [
    {"gkey": "nrhh_selection",
     "text": "Selected for a national honorary for service and community leadership."},
    {"gkey": "nrhh_events", "text": "Helped host staff-appreciation banquets."}]}

# A first draft that is itself broken (a key opened and abandoned after a text),
# then the corrected answer.
_BROKEN_DRAFT = (
    '{"bullets":[{"gkey":"end_to_end_pipeline","text":"Devised a job-discovery '
    'pipeline that loads postings to a master store.","},{"gkey":"two_stage_scoring",'
    '"text":"Created a two-stage LLM relevance filter."}]}\n\n'
    'Wait, correcting the format:\n\n'
    '{"bullets":[{"gkey":"end_to_end_pipeline","text":"Devised a job-discovery '
    'pipeline that loads postings to a master store."},{"gkey":"two_stage_scoring",'
    '"text":"Created a two-stage LLM relevance filter."}]}'
)


def test_the_corrected_answer_after_an_echoed_key_is_the_one_returned():
    assert llm._extract_json(_ECHOED_KEY) == _ECHOED_FINAL


def test_a_broken_first_draft_gives_way_to_the_correction():
    got = llm._extract_json(_BROKEN_DRAFT)
    assert [b["gkey"] for b in got["bullets"]] == ["end_to_end_pipeline",
                                                  "two_stage_scoring"]
    assert all(set(b) == {"gkey", "text"} for b in got["bullets"])


def test_a_cut_off_correction_leaves_the_complete_first_answer():
    first = '{"bullets":[{"gkey":"a1","text":"Built a fetcher."}]}'
    text = first + '\n\nWait, correcting:\n\n{"bullets":[{"gkey":"a1","text":"Bui'
    assert llm._extract_json(text) == {"bullets": [{"gkey": "a1",
                                                    "text": "Built a fetcher."}]}


def test_a_line_of_another_type_after_the_answer_is_not_a_correction():
    text = '{"bullets": []}\n\n[1] the list was empty.'
    assert llm._extract_json(text) == {"bullets": []}


def test_a_nested_object_of_a_pretty_printed_answer_is_never_taken_alone():
    """Only a value opening at the start of a line counts, so an indented bullet
    object of a broken pretty-printed answer cannot stand in for the answer; the
    older span fallback still reads what it always read."""
    text = '{\n  "bullets": [\n    {"gkey": "a1", "text": "Built."}\n  ],\n'
    assert llm._extract_json(text) == [{"gkey": "a1", "text": "Built."}]


@pytest.mark.parametrize("text,want", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here is the answer: {"a": 1} as asked.', {"a": 1}),
    ('[{"a": 1}]', [{"a": 1}]),
], ids=["plain", "fenced", "prose-around", "array"])
def test_a_single_answer_parses_as_before(text, want):
    assert llm._extract_json(text) == want


def test_no_json_at_all_is_still_bad_json():
    with pytest.raises(llm.LLMError) as err:
        llm._extract_json("Sorry, I cannot help with that.")
    assert err.value.kind == "bad_json"


def test_the_claude_lane_parses_a_self_corrected_reply_on_the_first_attempt(monkeypatch):
    """The cost the shape used to carry: a retry of the whole call."""
    slept: list = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    monkeypatch.setattr(llm, "_claude_cli",
                        lambda: types.SimpleNamespace(find_claude=lambda: "claude.exe"))
    attempts: list = []

    def _invoke(system, user, model, *, json_out, tools, timeout_s, tier=None):
        attempts.append(timeout_s)
        return claude_cli.CLIResult(_ECHOED_KEY, 10, 20)

    monkeypatch.setattr(llm, "_invoke_claude", _invoke)
    llm.reset_usage()
    try:
        out = llm._call_claude("sys", "user", "claude-sonnet-5", json_out=True)
        assert out == _ECHOED_FINAL
        assert len(attempts) == 1 and slept == []
        assert len(llm.USAGE) == 1
    finally:
        llm.reset_usage()


def test_the_repair_prompts_name_the_only_keys_an_answer_carries():
    """The other half: the reply shape the sweep asks for says that each bullet
    object holds two keys and that the JSON is written once."""
    user = sweep._user_prompt("Data Engineer", {"item": "x", "bullets": []},
                              sweep._SWEEP_CLOSING)
    assert 'only "gkey" and "text"' in user
    assert "once" in user
