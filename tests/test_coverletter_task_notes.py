"""A letter pass returns the letter and nothing else.

On 2026-09-28 a letter opened with the model's note about its own task
("Using avoid-ai-writing's own review output already provided, I'll finalize the
repaired body as-is since the audit confirms it's clean."). The transport fix
(claude_cli: no tools, skills or MCP servers) removed the cause; this pins the
backstop, `coverletter.drop_task_notes`, which every letter reply passes through
before the letter uses it.

No real LLM ever runs: compose.call is monkeypatched and the master is a
synthetic dict. Every letter here is made up.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import assets, compose, coverletter  # noqa: E402

LETTER = (
    "Globex's analysts query marts that break whenever an upstream feed changes shape. "
    "I spent a summer on exactly that problem.\n\n"
    "The nightly ETL at my last team ran for six hours. I rebuilt it in Python and the "
    "runtime fell 42%, so the morning reports landed on time.\n\n"
    "I would like to bring that habit of testing against old data to Globex's platform team."
)
TASK_NOTE = ("Using avoid-ai-writing's own review output already provided, I'll finalize "
                "the repaired body as-is since the audit confirms it's clean.")
BULLETS = {"a1": "Rebuilt the nightly ETL in Python and cut runtime 42%"}


@pytest.mark.parametrize("note", [
    TASK_NOTE,
    "Here is the revised letter body:",
    "Here's the rewritten cover letter.",
    "Sure, here's the rewrite.",
    "Certainly! I kept every fact and removed the flagged phrasing.",
    "**Rewritten body**",
    "## Revised draft",
    "No changes were needed; the audit found nothing to fix.",
])
def test_a_leading_note_is_dropped(note):
    assert coverletter.drop_task_notes(note + "\n\n" + LETTER) == LETTER


@pytest.mark.parametrize("note", [
    "Let me know if you'd like any further changes.",
    "I kept the letter as-is apart from the flagged phrases.",
    "---",
    "Issues found: none remaining after the second pass.",
])
def test_a_trailing_note_is_dropped(note):
    assert coverletter.drop_task_notes(LETTER + "\n\n" + note) == LETTER


@pytest.mark.parametrize("note", [
    "Let me know if you want any edits to the tone.",
    "Feel free to tell me if you'd like any tweaks.",
    "Let me know if you would like any revisions or adjustments.",
    "Let me know if you'd like me to adjust the tone or make any changes.",
])
def test_a_request_to_the_requester_about_edits_is_dropped_at_either_end(note):
    assert coverletter.drop_task_notes(LETTER + "\n\n" + note) == LETTER
    assert coverletter.drop_task_notes(note + "\n\n" + LETTER) == LETTER


@pytest.mark.parametrize("closing", [
    "I would welcome the chance to talk about the role. Please feel free to reach out if "
    "you need anything else from me.",
    "Thank you for your time. Feel free to contact me with any questions or if you need "
    "anything else.",
    "Let me know if there are any changes to the interview schedule.",
    "Please let me know if you would like references or any other materials.",
    "Feel free to call me with any adjustments to the start date in mind.",
])
def test_a_real_closing_paragraph_survives(closing):
    """A candidate's own closing offers contact. Only a note that asks the requester
    what they want changed in the letter is a note."""
    assert coverletter.drop_task_notes(LETTER + "\n\n" + closing) == LETTER + "\n\n" + closing


def test_a_skill_audit_report_keeps_only_the_letter():
    """The shape of the first leak, with made-up content: each report
    section's text sits in the same paragraph as its bold title."""
    report = (
        "Using **avoid-ai-writing** (rewrite mode) to strip the unsupported claim and "
        "check the rest of the letter again.\n\n"
        '**1. Issues found**\n- Unsupported claim: "Globex serves 3 million users"\n'
        '- "robust, scalable": stacked pair'
        "\n\n**2. Rewritten version**\n\n" + LETTER
        + "\n\n**3. What changed**\n- Deleted the unsourced Globex-scale sentence."
        + "\n\n**4. Second-pass audit**\nRe-read the full rewritten version: no residual patterns."
    )
    assert coverletter.drop_task_notes(report) == LETTER


def test_a_section_title_flattened_onto_its_text_is_dropped():
    report = (LETTER + "\n\n**3. What changed** - Deleted the unsourced sentence."
              "\n\n**4. Second-pass audit** Re-read the full rewritten version.")
    assert coverletter.drop_task_notes(report) == LETTER


def test_a_section_title_inside_the_last_paragraph_cuts_the_rest():
    report = LETTER + "\n**3. What changed**\n- Deleted the unsourced sentence."
    assert coverletter.drop_task_notes(report) == LETTER


def test_a_fenced_letter_loses_only_the_fence():
    assert coverletter.drop_task_notes("```text\n" + LETTER + "\n```") == LETTER


def test_a_real_letter_passes_through_byte_for_byte():
    """The words that give a note away stay legal inside the letter: an audit the
    candidate ran, a project that strips AI writing, a closing 'I will apply'
    and an opener that starts 'Here's what'."""
    letter = (
        "Here's what drew me to Globex: its records carry the same messy variation I "
        "handled at Initech.\n\n"
        "I audited 800 records from a previous quarter before anyone relied on the new "
        "model, and it caught 7% more of them.  INployed, my own project, strips AI-writing "
        "patterns from the letters it drafts.\n\n"
        "I will apply the same testing habit to Globex's pipelines.\n"
    )
    assert coverletter.drop_task_notes(letter) == letter


def test_a_reply_that_is_only_a_note_comes_back_empty():
    assert coverletter.drop_task_notes(TASK_NOTE) == ""
    assert coverletter.drop_task_notes("") == ""


# ── every letter pass runs its reply through the filter ──────────────────────
def _fake_call(monkeypatch, reply):
    monkeypatch.setattr(compose, "call", lambda system, user, *a, **k: reply)


def test_refine_drops_the_note(monkeypatch):
    _fake_call(monkeypatch, TASK_NOTE + "\n\n" + LETTER)
    assert coverletter.refine_body("Engineer", "Globex", "An older draft.", BULLETS) == LETTER


def test_a_refine_reply_that_is_only_a_note_keeps_the_draft(monkeypatch):
    _fake_call(monkeypatch, TASK_NOTE)
    assert coverletter.refine_body("Engineer", "Globex", "An older draft.", BULLETS) == "An older draft."


def test_the_grounding_repair_drops_the_note(monkeypatch):
    _fake_call(monkeypatch, TASK_NOTE + "\n\n" + LETTER)
    fixed = coverletter._repair_ungrounded_body("Engineer", "Globex", "Draft with Zyxcorp.",
                                                BULLETS, ["Zyxcorp"], "professional")
    assert fixed == LETTER


def test_the_style_repair_drops_the_note(monkeypatch):
    _fake_call(monkeypatch, TASK_NOTE + "\n\n" + LETTER)
    body = "I am thrilled to leverage my robust skills. " + LETTER
    assert coverletter.enforce_body_style("Engineer", "Globex", body, BULLETS) == LETTER


def test_the_first_draft_drops_the_note(monkeypatch):
    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"name": "Test User", "location": "NYC"}})
    seen = []

    def fake_call(system, user, *a, **k):
        seen.append(user)
        return TASK_NOTE + "\n\n" + LETTER if len(seen) == 1 else ""

    monkeypatch.setattr(compose, "call", fake_call)
    body = coverletter.generate_body("jd", "Engineer", "Globex", BULLETS)
    assert "avoid-ai-writing" not in body and "audit" not in body
    assert body.startswith("Globex's analysts")
