"""A JSON answer the model corrects in its own reply, on the scorer's path
(`claude_cli.extract_json_text`, which `ClaudePool.generate` runs for every
JSON-mode call `score_jobs.py` makes).

The Claude CLI at low effort checks its work in the reply itself: a complete
answer (often with a key nobody asked for), a line of prose ("Correction: ...
Final:"), then the corrected answer. The span from the first "{" to the last "}"
held both answers, so each such reply failed to parse and cost a retry. The reply
shapes are the ones `tests/test_llm_self_correction.py` copied from real runs,
plus the same shapes in the scorer's stage 1 and stage 2 answers. Nothing here
reaches a model.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import claude_cli  # noqa: E402

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

_BROKEN_DRAFT = (
    '{"bullets":[{"gkey":"end_to_end_pipeline","text":"Devised a job-discovery '
    'pipeline that loads postings to a master store.","},{"gkey":"two_stage_scoring",'
    '"text":"Created a two-stage LLM relevance filter."}]}\n\n'
    'Wait, correcting the format:\n\n'
    '{"bullets":[{"gkey":"end_to_end_pipeline","text":"Devised a job-discovery '
    'pipeline that loads postings to a master store."},{"gkey":"two_stage_scoring",'
    '"text":"Created a two-stage LLM relevance filter."}]}'
)

# The scorer's stage 1 answer with an echoed key, the note, and the correction.
_STAGE1 = (
    '{"score": 7, "reason": "Python data pipelines match the role.", "max_chars": 200}\n\n'
    'Correction: only "score" and "reason". Final:\n\n'
    '{"score": 7, "reason": "Python data pipelines match the role."}'
)
# The scorer's stage 2 answer, revised in the same reply.
_STAGE2 = (
    '{"deep_score": 8, "strengths": ["ETL in Python"], "gaps": ["No Spark"], '
    '"recommendation": "apply"}\n\n'
    'Wait, the gaps should name the missing cloud work too. Final:\n\n'
    '{"deep_score": 7, "strengths": ["ETL in Python"], "gaps": ["No Spark", '
    '"No AWS"], "recommendation": "apply"}'
)


def _read(text):
    return json.loads(claude_cli.extract_json_text(text))


def test_the_corrected_answer_after_an_echoed_key_is_the_one_returned():
    assert _read(_ECHOED_KEY) == _ECHOED_FINAL


def test_a_broken_first_draft_gives_way_to_the_correction():
    got = _read(_BROKEN_DRAFT)
    assert [b["gkey"] for b in got["bullets"]] == ["end_to_end_pipeline",
                                                  "two_stage_scoring"]
    assert all(set(b) == {"gkey", "text"} for b in got["bullets"])


def test_a_stage1_answer_reads_its_correction():
    assert _read(_STAGE1) == {"score": 7,
                              "reason": "Python data pipelines match the role."}


def test_a_stage2_answer_reads_its_revision():
    got = _read(_STAGE2)
    assert got["deep_score"] == 7
    assert got["gaps"] == ["No Spark", "No AWS"]


def test_a_cut_off_correction_leaves_the_complete_first_answer():
    first = '{"score": 6, "reason": "Close match."}'
    text = first + '\n\nWait, correcting:\n\n{"score": 5, "reas'
    assert _read(text) == {"score": 6, "reason": "Close match."}


def test_a_numbered_note_after_the_answer_is_not_a_correction():
    assert _read('{"score": 4, "reason": "Thin."}\n\n[1] the list was empty.') == {
        "score": 4, "reason": "Thin."}
    assert _read('[{"gkey": "a"}]\n[2] done') == [{"gkey": "a"}]


def test_a_note_line_before_the_answer_leaves_the_answer():
    text = '[1] lengths checked.\n{"score": 3, "reason": "Junior role."}'
    assert _read(text) == {"score": 3, "reason": "Junior role."}


def test_a_short_value_of_another_type_never_stands_in_for_the_answer():
    text = '{"score": 9, "reason": "Strong.", "x": 1}\nCheck:\n[1]\n'
    assert _read(text) == {"score": 9, "reason": "Strong.", "x": 1}


def test_a_nested_object_of_a_pretty_printed_answer_is_never_taken_alone():
    text = '{\n  "bullets": [\n    {"gkey": "a1", "text": "Built."}\n  ],\n'
    assert _read(text) == [{"gkey": "a1", "text": "Built."}]


def test_an_unindented_broken_pretty_print_is_read_whole_as_before():
    text = ('{\n"bullets": [\n{"gkey": "a1", "text": "Built."},\n'
            '{"gkey": "a2", "text": "Ran."}\n],\n')
    assert _read(text) == [{"gkey": "a1", "text": "Built."},
                           {"gkey": "a2", "text": "Ran."}]


def test_no_json_at_all_still_raises_value_error():
    with pytest.raises(ValueError):
        claude_cli.extract_json_text("Sorry, I cannot help with that.")


def test_the_pool_parses_a_self_corrected_reply_on_the_first_attempt(monkeypatch):
    """The cost the shape used to carry on the scorer: a retry of the whole call."""
    slept: list = []

    async def _no_sleep(s):
        slept.append(s)

    calls = {"n": 0}

    def fake_run_claude(system, user, model, *, json_mode=False, allow_websearch=False,
                        timeout_s=claude_cli.DEFAULT_TIMEOUT_S):
        calls["n"] += 1
        return claude_cli.CLIResult(_STAGE1, 10, 20, 0, 0)

    monkeypatch.setattr(claude_cli.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    pool = claude_cli.ClaudePool(max_procs=1)
    config = SimpleNamespace(system_instruction="sys",
                             response_mime_type="application/json", response_schema=None)
    resp = asyncio.run(pool.generate(model="m", contents="job", config=config))
    assert json.loads(resp.text) == {"score": 7,
                                     "reason": "Python data pipelines match the role."}
    assert calls["n"] == 1 and slept == []
