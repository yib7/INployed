"""Cycle 21, Task 5: the candidate text in the stage 1, stage 2 and writer prompts.

The three scorer prompts used to say "the candidate graduated May 2026" in fixed
text. They now take that text from the candidate profile the user sets in the
dashboard: candidate_prompt_vars(profile) returns nine strings, and the three
*_TEMPLATE_RESUME templates carry them as placeholders. Every placeholder sits in
the RESUME half, so the Claude lane's cached system prompt stays identical for
every job in a run.

At the default profile (Finished school, May 2026, no clearance) stage 2 and the
writer render byte-identical to before this cycle; stage 1 changes (its new
eligibility line, its shorter candidate paragraph).

Every test runs against the sandboxed scoring constants that conftest rebinds, so
the author's scoring_config.json never leaks in. Every pool below is a fake.
"""
import asyncio
import json
import re
import string
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import score_jobs as sj  # noqa: E402

TODAY = date(2026, 9, 29)

KEYS = ("status_dates", "status_intro", "target", "eligibility_rule", "status_closing",
        "clearance_context", "stage2_status", "clearance_gap_note", "writer_status")

STATUSES = ("finished", "undergrad", "grad")
# A graduation month that is consistent with TODAY for each status: a finished
# candidate graduated already, an in-school one has not.
GRADUATION = {"finished": "May 2026", "undergrad": "May 2027", "grad": "December 2027"}
# An in-school profile keeps its month far in the future so a test that goes
# through score_stage1 (which reads the real clock) never sees it roll over.
FAR_FUTURE = "May 2099"

# (clearance label, open to a clearance the employer sponsors), every case.
CLEARANCES = [(label, open_) for label in sj.CLEARANCE_LEVELS for open_ in (False, True)]

ALL_CASES = [(status, with_date, label, open_)
             for status in STATUSES for with_date in (True, False)
             for label, open_ in CLEARANCES]


def _profile(status, graduation, clearance="None", sponsorship=False):
    return sj.candidate_profile(status=status, graduation=graduation, clearance=clearance,
                                sponsorship=sponsorship, today=TODAY)


def _case_profile(status, with_date, label, open_):
    grad = GRADUATION[status] if with_date else ""
    profile = _profile(status, grad, label, open_)
    # The resolver must keep what the case asked for, or the case tests nothing.
    assert profile.status == status
    assert profile.graduation_text == (grad or None)
    assert profile.clearance_label == label
    return profile


def _vars(status="finished", with_date=True, label="None", open_=False):
    return sj.candidate_prompt_vars(_case_profile(status, with_date, label, open_))


def _render_all(v):
    """The three full prompts as the Gemini lane sends them."""
    return {
        "stage1": sj.STAGE1_TEMPLATE.format(resume="R", job="J", today="T", **v),
        "stage2": sj.STAGE2_TEMPLATE.format(resume="R", job="J", today="T", **v),
        "writer": (sj.WRITER_TEMPLATE_RESUME.format(resume="R", today="T", **v)
                   + sj.WRITER_TEMPLATE_JOB.format(findings="F", job="J")),
    }


# --- the variables ----------------------------------------------------------------

@pytest.mark.parametrize("status,with_date,label,open_", ALL_CASES)
def test_every_key_is_present_for_every_status_date_and_clearance(status, with_date, label, open_):
    v = _vars(status, with_date, label, open_)
    assert set(v) == set(KEYS)
    assert all(isinstance(text, str) for text in v.values())
    for key in KEYS:
        if key in ("clearance_context", "clearance_gap_note"):
            continue
        assert v[key].strip(), key


@pytest.mark.parametrize("status,with_date,label,open_", ALL_CASES)
def test_no_variable_text_has_a_banned_writing_shape(status, with_date, label, open_):
    for key, text in _vars(status, with_date, label, open_).items():
        assert "—" not in text, key
        assert " -- " not in text, key
        for shape in (", not ", "rather than", "instead of", "not just"):
            assert shape not in text, (key, shape)


@pytest.mark.parametrize("status,with_date,label,open_", ALL_CASES)
def test_no_rendered_template_leaves_a_placeholder_unfilled(status, with_date, label, open_):
    for name, text in _render_all(_vars(status, with_date, label, open_)).items():
        assert "{" not in text and "}" not in text, name


@pytest.mark.parametrize("status", STATUSES)
def test_the_graduation_month_appears_in_each_status_line_only_when_there_is_one(status):
    grad = GRADUATION[status]
    with_date = _vars(status, True)
    without = _vars(status, False)
    for key in ("status_dates", "status_intro", "eligibility_rule", "stage2_status",
                "writer_status"):
        assert grad in with_date[key], key
    year = re.compile(r"\b(?:19|20)\d\d\b")
    for key, text in without.items():
        assert not year.search(text), key
        assert "None" not in text, key


@pytest.mark.parametrize("with_date", [True, False])
def test_an_in_school_status_never_calls_the_candidate_a_graduate(with_date):
    for status in ("undergrad", "grad"):
        text = " ".join(_vars(status, with_date).values())
        assert "COMPLETED" not in text
        assert "new graduate" not in text
        assert "already in the PAST" not in text
        assert "available to start immediately" not in text


@pytest.mark.parametrize("with_date", [True, False])
def test_a_finished_status_never_calls_the_candidate_a_student(with_date):
    text = " ".join(_vars("finished", with_date).values())
    assert "CURRENT undergraduate" not in text
    assert "CURRENT graduate" not in text
    assert "current undergraduate" not in text
    assert "current graduate" not in text


def test_the_target_follows_the_status():
    assert _vars("finished")["target"] == "ENTRY-LEVEL and EARLY-CAREER roles"
    assert _vars("undergrad")["target"] == "INTERNSHIPS, CO-OPS and ENTRY-LEVEL roles"
    assert _vars("grad")["target"] == "INTERNSHIPS, CO-OPS and ENTRY-LEVEL roles"


def test_the_default_profile_reads_as_a_finished_candidate_with_no_clearance():
    v = sj.candidate_prompt_vars(sj.candidate_profile(today=TODAY))
    assert v == sj.candidate_prompt_vars(sj.candidate_profile(
        status="finished", graduation="May 2026", clearance="None", sponsorship=False,
        today=TODAY))
    assert v["clearance_context"] == ""
    assert v["clearance_gap_note"] == ""
    assert "May 2026" in v["status_dates"]


def test_an_unknown_status_code_reads_as_finished():
    profile = sj.CandidateProfile(status="alumnus", graduation=None, graduation_text=None,
                                  clearance_rank=0, clearance_label="None", sponsorship=False)
    assert sj.candidate_prompt_vars(profile) == _vars("finished", False)


# --- clearance text ------------------------------------------------------------------

_NONE_OPEN_CONTEXT = (
    "SECURITY CLEARANCE: The candidate holds no security clearance and is open to getting "
    "one through the employer. A posting that sponsors a clearance or asks for the ability "
    "to obtain one is NOT a gap: judge it on skills like any other role. A posting that "
    "needs an active clearance at hire is a hard requirement the candidate cannot meet "
    "(score 1).\n\n")
_TSSCI_CONTEXT = (
    "SECURITY CLEARANCE: The candidate holds an active TS/SCI clearance. A posting that "
    "needs any clearance level, or the ability to obtain one, is met and is NOT a gap.\n\n")


def _held_context(label):
    return (f"SECURITY CLEARANCE: The candidate holds an active {label} clearance. A posting "
            f"that needs {label} or a lower level, or the ability to obtain one of those, is "
            "met and is NOT a gap. A posting that needs a higher level is a hard requirement "
            "the candidate cannot meet (score 1).\n\n")


def _held_open_context(label):
    return (f"SECURITY CLEARANCE: The candidate holds an active {label} clearance and is open "
            f"to a higher level through the employer. A posting that needs {label} or a lower "
            "level is met and is NOT a gap, and so is a higher level the employer sponsors or "
            "asks the candidate to obtain. A higher level needed active at hire is a hard "
            "requirement the candidate cannot meet (score 1).\n\n")


_NONE_OPEN_NOTE = (" The candidate is open to getting a clearance through the employer, so a "
                   "clearance the employer sponsors or asks the candidate to obtain is never "
                   "a gap.")
_TSSCI_NOTE = " The candidate holds an active TS/SCI clearance, so a required clearance is never a gap."


def _held_note(label):
    return (f" The candidate holds an active {label} clearance, so a clearance at or below "
            f"{label} is never a gap.")


def _held_open_note(label):
    return (f" The candidate holds an active {label} clearance and is open to a higher level, "
            f"so a clearance at or below {label}, or a higher level the employer sponsors, is "
            "never a gap.")


def test_no_clearance_and_not_open_adds_no_clearance_text():
    v = _vars(label="None", open_=False)
    assert v["clearance_context"] == ""
    assert v["clearance_gap_note"] == ""
    for text in _render_all(v).values():
        assert "SECURITY CLEARANCE" not in text
        assert "holds an active" not in text
        assert "open to getting a clearance" not in text


def test_no_clearance_but_open_states_the_sponsorship_rule():
    v = _vars(label="None", open_=True)
    assert v["clearance_context"] == _NONE_OPEN_CONTEXT
    assert v["clearance_gap_note"] == _NONE_OPEN_NOTE


def test_a_ts_sci_holder_needs_no_level_check_open_or_not():
    for open_ in (False, True):
        v = _vars(label="TS/SCI", open_=open_)
        assert v["clearance_context"] == _TSSCI_CONTEXT
        assert v["clearance_gap_note"] == _TSSCI_NOTE


@pytest.mark.parametrize("label", ["Public Trust", "Secret", "Top Secret"])
def test_a_held_clearance_names_its_level_and_the_higher_level_rule(label):
    held = _vars(label=label, open_=False)
    assert held["clearance_context"] == _held_context(label)
    assert held["clearance_gap_note"] == _held_note(label)
    open_ = _vars(label=label, open_=True)
    assert open_["clearance_context"] == _held_open_context(label)
    assert open_["clearance_gap_note"] == _held_open_note(label)


@pytest.mark.parametrize("label,open_", [c for c in CLEARANCES if c != ("None", False)])
def test_every_prompt_renders_a_clearance_case(label, open_):
    rendered = _render_all(_vars(label=label, open_=open_))
    v = _vars(label=label, open_=open_)
    # Stage 1 carries the paragraph in front of the experience bar.
    assert v["clearance_context"] in rendered["stage1"]
    assert "SECURITY CLEARANCE:" in rendered["stage1"]
    assert rendered["stage1"].index("SECURITY CLEARANCE:") < rendered["stage1"].index(
        "required-experience bar")
    # Stage 2 and the writer carry the one-sentence gap note.
    assert v["clearance_gap_note"] in rendered["stage2"]
    assert v["clearance_gap_note"] in rendered["writer"]
    if label not in ("None", "TS/SCI"):
        for text in rendered.values():
            assert f"active {label} clearance" in text


# --- the templates -----------------------------------------------------------------

def _fields(template):
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_every_candidate_placeholder_sits_in_a_resume_half():
    """The Claude lane caches the RESUME half, so a placeholder in a JOB half would
    make the cached prompt differ per job (and never resolve there)."""
    allowed = set(KEYS) | {"today", "resume"}
    for template in (sj.STAGE1_TEMPLATE_RESUME, sj.STAGE2_TEMPLATE_RESUME,
                     sj.WRITER_TEMPLATE_RESUME):
        assert _fields(template) <= allowed
    assert _fields(sj.STAGE1_TEMPLATE_JOB) == {"job"}
    assert _fields(sj.STAGE2_TEMPLATE_JOB) == {"job"}
    assert _fields(sj.WRITER_TEMPLATE_JOB) == {"findings", "job"}
    used = _fields(sj.STAGE1_TEMPLATE_RESUME) | _fields(sj.STAGE2_TEMPLATE_RESUME) \
        | _fields(sj.WRITER_TEMPLATE_RESUME)
    assert set(KEYS) <= used


def test_no_template_names_a_graduation_month_or_a_school_status_in_fixed_text():
    for template in (sj.STAGE1_TEMPLATE_RESUME, sj.STAGE2_TEMPLATE_RESUME,
                     sj.WRITER_TEMPLATE_RESUME):
        assert "May 2026" not in template
        assert "graduated" not in template
    assert "post-graduation" not in sj.STAGE1_TEMPLATE_RESUME
    assert "B.S. Computer Science" not in sj.STAGE1_TEMPLATE_RESUME


def test_today_str_docstring_no_longer_names_a_month():
    assert "May 2026" not in sj.today_str.__doc__


def test_stage_one_for_a_finished_candidate_screens_out_current_student_postings():
    text = _render_all(_vars("finished"))["stage1"]
    assert "internship or co-op for current students" in text
    assert "May 2026" in text
    assert "ENTRY-LEVEL and EARLY-CAREER roles" in text
    assert "This candidate is a new graduate (graduated May 2026, available to start " \
           "immediately) with one strong data-science internship" in text
    assert "the candidate only graduated in May 2026; they are a graduate, available " \
           "immediately." in text


def test_stage_one_without_a_graduation_month_still_names_the_screen():
    text = _render_all(_vars("finished", with_date=False))["stage1"]
    assert "internship or co-op for current students" in text
    assert "This candidate is a new graduate (available to start immediately) with one" in text
    assert "the candidate only recently finished school; they are a graduate" in text


def test_stage_one_for_an_undergraduate_keeps_internships_in_scope():
    text = _render_all(_vars("undergrad"))["stage1"]
    assert "Internships and co-ops for current undergraduates are in scope" in text
    assert "expected to graduate in May 2027" in text
    assert "INTERNSHIPS, CO-OPS and ENTRY-LEVEL roles" in text
    assert "just because the candidate is still an undergraduate student." in text
    assert "internship or co-op for current students" not in text


def test_stage_one_for_a_graduate_student_keeps_internships_in_scope():
    text = _render_all(_vars("grad"))["stage1"]
    assert "Internships and co-ops for graduate students are in scope" in text
    assert "expected to finish in December 2027" in text
    assert "just because the candidate is still a graduate student." in text
    assert "only for undergraduate students" in text


@pytest.mark.parametrize("with_date", [True, False])
def test_stage_one_for_a_graduate_student_skips_the_advanced_degree_clause(with_date):
    v = _vars("grad", with_date)
    text = _render_all(v)["stage1"]
    skip = "skip the advanced-degree clause above"
    assert skip in v["eligibility_rule"]
    # The skip sentence sits between the clause it withdraws and the next section.
    bullet = text.index("- Also score 1-2 for a hard advanced-degree requirement")
    assert bullet < text.index(skip) < text.index("ADJACENT ANALYTICAL ROLES ARE IN-DOMAIN")
    assert "a posting that asks for a master's or PhD stays in scope" in text
    assert "judged on skills, stack and domain" in v["eligibility_rule"]


@pytest.mark.parametrize("status", ["finished", "undergrad"])
@pytest.mark.parametrize("with_date", [True, False])
def test_stage_one_keeps_the_advanced_degree_clause_for_every_other_status(status, with_date):
    text = _render_all(_vars(status, with_date))["stage1"]
    assert "skip the advanced-degree clause" not in text
    assert "a hard advanced-degree requirement the candidate lacks" in text


def test_stage_one_places_the_eligibility_rule_after_the_advanced_degree_bullet():
    v = _vars("undergrad")
    text = _render_all(v)["stage1"]
    bullet = text.index("- Also score 1-2 for a hard advanced-degree requirement")
    rule = text.index(v["eligibility_rule"])
    adjacent = text.index("ADJACENT ANALYTICAL ROLES ARE IN-DOMAIN")
    assert bullet < rule < adjacent
    # One line: the bullet's own line ends, then the rule's line, then a blank line.
    assert "\n" + v["eligibility_rule"] + "\n\nADJACENT" in text


def test_stage_two_and_the_writer_state_the_status():
    for status, phrase in (("undergrad", "current undergraduate student"),
                           ("grad", "current graduate student")):
        rendered = _render_all(_vars(status))
        assert phrase in rendered["stage2"]
        assert phrase in rendered["writer"]
        assert "May 2027" in rendered["stage2"] or "December 2027" in rendered["stage2"]
    finished = _render_all(_vars("finished"))
    assert "the candidate's May 2026 graduation is in the past" in finished["stage2"]
    assert "The candidate graduated in May 2026 and the degree is complete" in finished["writer"]


def test_the_writer_keeps_its_style_pins():
    assert "no em dashes and no hype" in sj.WRITER_TEMPLATE_RESUME
    for kw in ("relocat", "on-site", "hybrid", "remote", "time zone", "work authorization",
               "sponsorship", "career path", "business background"):
        assert kw in sj.WRITER_TEMPLATE_RESUME.lower(), kw


# The WRITER_TEMPLATE_RESUME text before cycle 21, with {resume} and {today} left as
# placeholders. Stage 2 has the same pin in test_score_jobs_claude.py
# (_FROZEN_STAGE2_TEMPLATE); the writer had none, so it lives here. At the default
# profile the placeholder-carrying template must render to exactly this text.
_FROZEN_WRITER_RESUME = """\
Another system has already judged how well this job fits the candidate. Its findings come after the resume. Write the notes that explain those findings to the candidate. The scores and the recommendation are final.

TODAY'S DATE IS {today}. The candidate graduated in May 2026 and the degree is complete, so graduation timing is never a gap.

Write three fields:
- "reason": one or two sentences on why the job got its fit score, naming what decided it (skills and tools, the field, the experience the job asks for).
- "strengths": two to five items. Each ties something specific in the resume (the internship, a project, coursework, a tool) to a specific requirement or duty in the job.
- "gaps": zero to five items. Each is a concrete, stated requirement the candidate does not meet: a tool or technology they lack, a hard credential such as a required clearance or advanced degree, or required years of experience. Take them from the unmet requirement lines in the findings, and never list a line the findings mark as met. An empty list is fine.

Never list location, on-site, hybrid or remote terms, relocation, time zone, or work authorization and visa sponsorship as a gap. For analytical roles (data, business, BI, reporting, analytics, product or operations analyst, data scientist), never list career path, business background, degree field or job-title history as a gap. Write plain, specific sentences with no em dashes and no hype.

Resume:
---
{resume}
---

"""


def test_the_writer_renders_byte_identical_at_the_default_profile():
    v = sj.candidate_prompt_vars(sj.candidate_profile(today=TODAY))
    rendered = sj.WRITER_TEMPLATE_RESUME.format(resume="{resume}", today="{today}", **v)
    assert rendered == _FROZEN_WRITER_RESUME


# --- the call sites ------------------------------------------------------------------

class _Pool:
    """Records what score_stage1, score_stage2 and write_notes send; never networks."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(
            contents=contents, system=getattr(config, "system_instruction", None)))
        return SimpleNamespace(
            text=json.dumps(self.payload),
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1))


_STAGE1_PAYLOAD = {"score": 4, "reason": "fit"}
_STAGE2_PAYLOAD = {"deep_score": 8, "strengths": ["s"], "gaps": ["g"], "recommendation": "apply"}
_WRITER_PAYLOAD = {"reason": "r", "strengths": ["s"], "gaps": ["g"]}


def _run(which, pool, resume="RESUME BODY", job="JOB BODY"):
    sem = asyncio.Semaphore(1)
    if which == "stage1":
        return asyncio.run(sj.score_stage1(pool, sem, resume, "J1", job))
    if which == "stage2":
        return asyncio.run(sj.score_stage2(pool, sem, resume, "J1", job))
    return asyncio.run(sj.write_notes(pool, sem, resume, "J1", job, "FINDINGS BODY"))


_PAYLOADS = {"stage1": _STAGE1_PAYLOAD, "stage2": _STAGE2_PAYLOAD, "writer": _WRITER_PAYLOAD}
_CANDIDATE_PHRASE = {
    "stage1": "Internships and co-ops for current undergraduates are in scope",
    "stage2": "the candidate is a current undergraduate student",
    "writer": "The candidate is a current undergraduate student",
}


def _set_undergrad_profile(monkeypatch):
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", FAR_FUTURE)
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Secret")
    monkeypatch.setattr(sj, "CLEARANCE_SPONSORSHIP", True)


@pytest.mark.parametrize("which", ["stage1", "stage2", "writer"])
def test_the_gemini_prompt_carries_the_configured_candidate(monkeypatch, which):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    _set_undergrad_profile(monkeypatch)
    pool = _Pool(_PAYLOADS[which])
    _run(which, pool)
    contents = pool.calls[0].contents
    assert _CANDIDATE_PHRASE[which] in contents
    assert FAR_FUTURE in contents
    assert "active Secret clearance and is open to a higher level" in contents
    assert "{" not in contents


@pytest.mark.parametrize("which", ["stage1", "stage2", "writer"])
def test_the_claude_system_prompt_carries_the_candidate_and_the_job_half_does_not(
        monkeypatch, which):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "claude")
    _set_undergrad_profile(monkeypatch)
    pool = _Pool(_PAYLOADS[which])
    _run(which, pool, job="JOB A")
    _run(which, pool, job="JOB B")
    first, second = pool.calls
    assert _CANDIDATE_PHRASE[which] in first.system
    assert FAR_FUTURE in first.system
    assert "active Secret clearance" in first.system
    # The cached half is identical for two different jobs, so the cache holds.
    assert first.system == second.system
    # The job half carries no candidate text.
    for text in (first.contents, second.contents):
        assert FAR_FUTURE not in text
        assert "clearance" not in text
    assert "JOB A" in first.contents and "JOB B" in second.contents


@pytest.mark.parametrize("which", ["stage1", "stage2", "writer"])
def test_the_default_profile_sends_the_finished_candidate_text(monkeypatch, which):
    monkeypatch.setattr(sj, "SCORING_PROVIDER", "gemini")
    pool = _Pool(_PAYLOADS[which])
    _run(which, pool)
    contents = pool.calls[0].contents
    assert "May 2026" in contents
    assert "SECURITY CLEARANCE" not in contents
