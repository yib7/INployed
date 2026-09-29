"""Cycle 21, Task 8: the Jev scorer follows the candidate profile.

`jev_score.candidate_for(profile)` renders the candidate block of every Jev
request from the four profile settings (school status, graduation month, held
clearance, open to a clearance the employer sponsors). stage 1 gains a
"not eligible" main factor and a cap for it, the clearance wording says "the
candidate does not hold", and `score_jobs.run_scoring` hands the one resolved
profile to both stages and to `jev_facts`.

Hermetic: every judge is scripted, every pool is a fake, no key, no network.
"""
import asyncio
import itertools
from datetime import date

import pytest

import jev
import jev_score
import score_jobs as sj
from test_jev_score import (JOB2_MD, JOB_MD, NO_FACTS, RESUME, RecordingPool, ScriptedJudge,
                            _jobs_df, _reads)

TODAY = date(2026, 9, 29)
KEYS = ["status", "eligibility", "experience", "target", "clearance", "does_not_count"]
STATUSES = ("finished", "undergrad", "grad")
LEVELS = ("None", "Public Trust", "Secret", "Top Secret", "TS/SCI")
GRAD = "May 2027"

DOES_NOT_COUNT = ("Location, on-site, hybrid or remote terms, relocation, time zone, "
                  "visa sponsorship and work authorization never count for or against "
                  "a job. The candidate will relocate and is authorized to work in the "
                  "U.S. without sponsorship.")

STATUS_TEXT = {
    ("finished", True): "Finished school: graduated {g} and available to start now.",
    ("finished", False): "Finished school and available to start now.",
    ("undergrad", True): "In school: an undergraduate student, expected to graduate in {g}.",
    ("undergrad", False): "In school: an undergraduate student.",
    ("grad", True): ("In school: a graduate student (master's or PhD), expected to finish "
                     "in {g}."),
    ("grad", False): "In school: a graduate student (master's or PhD).",
}
ELIGIBILITY_TEXT = {
    ("finished", True): (
        "Jobs open only to current students (internships, co-ops, or a rule to be enrolled "
        "in a degree program) exclude the candidate, and so do jobs limited to a graduation "
        "window that {g} falls outside."),
    ("finished", False): (
        "Jobs open only to current students (internships, co-ops, or a rule to be enrolled "
        "in a degree program) exclude the candidate."),
    ("undergrad", True): (
        "Internships and co-ops for undergraduates are open to the candidate. Jobs open only "
        "to graduate students (master's or PhD) exclude the candidate, and so do jobs limited "
        "to a graduation window that {g} falls outside."),
    ("undergrad", False): (
        "Internships and co-ops for undergraduates are open to the candidate. Jobs open only "
        "to graduate students (master's or PhD) exclude the candidate."),
    ("grad", True): (
        "Internships and co-ops for graduate students are open to the candidate. Jobs open "
        "only to undergraduate students exclude the candidate, and so do jobs limited to a "
        "graduation window that {g} falls outside."),
    ("grad", False): (
        "Internships and co-ops for graduate students are open to the candidate. Jobs open "
        "only to undergraduate students exclude the candidate."),
}
EXPERIENCE_FINISHED = ("One data-science internship plus academic and personal projects. "
                       "No full-time work since graduating.")
EXPERIENCE_IN_SCHOOL = ("One data-science internship plus academic and personal projects. "
                        "No full-time work yet.")
TARGET_FINISHED = "Entry-level and early-career roles."
TARGET_IN_SCHOOL = "Internships, co-ops and entry-level roles."


def _clearance_text(label, open_):
    if label == "None":
        if open_:
            return ("Holds no security clearance and is open to getting one through the "
                    "employer: a job that sponsors a clearance or asks for the ability to "
                    "obtain one is open to the candidate.")
        return "Holds no security clearance."
    if label == "TS/SCI":
        return ("Holds an active TS/SCI clearance: a job that needs any clearance level is "
                "open to the candidate.")
    if open_:
        return (f"Holds an active {label} clearance and is open to a higher level through the "
                f"employer: a job that needs {label} or a lower level is open to the "
                "candidate, and so is a higher level the employer sponsors.")
    return (f"Holds an active {label} clearance: a job that needs {label} or a lower level is "
            "open to the candidate.")


def _expected(status, graduation, label, open_):
    """The six strings for a status, a graduation month text ("" for none), a
    clearance label and the open box, as the brief words them."""
    g, with_grad = graduation, bool(graduation)
    in_school = status != "finished"
    return {
        "status": STATUS_TEXT[status, with_grad].format(g=g),
        "eligibility": ELIGIBILITY_TEXT[status, with_grad].format(g=g),
        "experience": EXPERIENCE_IN_SCHOOL if in_school else EXPERIENCE_FINISHED,
        "target": TARGET_IN_SCHOOL if in_school else TARGET_FINISHED,
        "clearance": _clearance_text(label, open_),
        "does_not_count": DOES_NOT_COUNT,
    }


CASES = list(itertools.product(STATUSES, (True, False), LEVELS, (False, True)))


def _profile(status="finished", graduation="May 2026", clearance="None", sponsorship=False):
    return {"status": status, "graduation": graduation, "clearance": clearance,
            "sponsorship": sponsorship}


# --- candidate_for: every case, exact text -------------------------------------------

@pytest.mark.parametrize("status,with_grad,label,open_", CASES,
                         ids=[f"{s}-{'G' if g else 'noG'}-{c}-{'open' if o else 'shut'}"
                              for s, g, c, o in CASES])
def test_candidate_for_renders_every_case_word_for_word(status, with_grad, label, open_):
    graduation = GRAD if with_grad else ""
    got = jev_score.candidate_for(_profile(status, graduation, label, open_))
    assert list(got) == KEYS
    assert got == _expected(status, graduation, label, open_)


def test_the_default_candidate_is_finished_school_may_2026_and_no_clearance():
    assert jev_score.CANDIDATE == jev_score.candidate_for(None)
    assert jev_score.CANDIDATE == _expected("finished", "May 2026", "None", False)
    assert jev_score.CANDIDATE["status"] == (
        "Finished school: graduated May 2026 and available to start now.")
    assert list(jev_score.CANDIDATE) == KEYS


@pytest.mark.parametrize("bad", [None, {}, "finished", 7, ["undergrad"], (), 3.5])
def test_candidate_for_reads_a_missing_or_bad_profile_as_the_defaults(bad):
    assert jev_score.candidate_for(bad) == jev_score.CANDIDATE


def test_candidate_for_reads_an_unknown_status_or_clearance_as_the_default():
    default = jev_score.candidate_for(_profile())
    for status in ("sophomore", "", None, 3, "In school"):
        assert jev_score.candidate_for(_profile(status=status)) == default, status
    for clearance in ("Cosmic", "", None, 4, "Secret Squirrel"):
        assert jev_score.candidate_for(_profile(clearance=clearance)) == default, clearance


def test_candidate_for_reads_a_key_the_mapping_lacks_as_its_default():
    assert jev_score.candidate_for({"status": "undergrad"}) == jev_score.candidate_for(
        _profile(status="undergrad"))
    assert jev_score.candidate_for({"clearance": "Secret"}) == jev_score.candidate_for(
        _profile(clearance="Secret"))
    assert jev_score.candidate_for({"graduation": "June 2028"}) == jev_score.candidate_for(
        _profile(graduation="June 2028"))


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_graduation_reads_as_no_graduation_month(blank):
    """score_jobs.jev_profile() carries None when the month is unknown; a blank
    string is the same. Both take the no-month strings and skip the May 2026 default."""
    got = jev_score.candidate_for(_profile(status="undergrad", graduation=blank))
    assert got == _expected("undergrad", "", "None", False)
    got = jev_score.candidate_for(_profile(status="finished", graduation=blank))
    assert got == _expected("finished", "", "None", False)


def test_candidate_for_folds_case_and_spacing_in_a_status_or_clearance():
    assert jev_score.candidate_for(_profile(status="  UNDERGRAD ", graduation=GRAD,
                                            clearance="top secret")
                                   ) == _expected("undergrad", GRAD, "Top Secret", False)
    assert jev_score.candidate_for(_profile(status="Grad", graduation=GRAD, clearance=" ts/sci")
                                   ) == _expected("grad", GRAD, "TS/SCI", False)


@pytest.mark.parametrize("value,open_", [(True, True), (False, False), ("true", True),
                                         ("yes", True), ("false", False), ("maybe", False),
                                         (None, False), (1, True), (0, False)])
def test_candidate_for_reads_sponsorship_the_way_the_settings_read_a_switch(value, open_):
    got = jev_score.candidate_for(_profile(clearance="Secret", sponsorship=value))
    assert got["clearance"] == _clearance_text("Secret", open_)


def test_candidate_for_returns_a_fresh_dict_each_call():
    first = jev_score.candidate_for(None)
    first["status"] = "changed"
    assert jev_score.candidate_for(None)["status"] != "changed"
    assert jev_score.CANDIDATE["status"] != "changed"


def test_jev_score_knows_the_same_clearance_levels_as_score_jobs():
    """jev_score never imports score_jobs, so it carries its own copy of the
    levels; this holds the two equal."""
    assert tuple(jev_score.CLEARANCE_LEVELS) == tuple(sj.CLEARANCE_LEVELS) == LEVELS


def test_candidate_for_takes_what_the_resolved_profile_hands_it():
    """A CandidateProfile.jev_profile() mapping renders without a note or a gap:
    the status code, the month text or None, the label and the box."""
    for status, grad, label, open_ in [("finished", "May 2026", "None", False),
                                       ("undergrad", "May 2027", "Secret", True),
                                       ("grad", "December 2027", "TS/SCI", False),
                                       ("undergrad", "", "Public Trust", False)]:
        profile = sj.candidate_profile(status=status, graduation=grad, clearance=label,
                                       sponsorship=open_, today=TODAY)
        got = jev_score.candidate_for(profile.jev_profile())
        assert got["status"] == STATUS_TEXT[status, bool(grad)].format(g=grad)
        assert got["clearance"] == _clearance_text(label, open_)


def test_a_rolled_over_in_school_profile_renders_as_finished_school():
    profile = sj.candidate_profile(status="undergrad", graduation="May 2026",
                                   clearance="None", sponsorship=False, today=TODAY)
    assert profile.rolled_over
    got = jev_score.candidate_for(profile.jev_profile())
    assert got["status"] == "Finished school: graduated May 2026 and available to start now."


# --- the constants ---------------------------------------------------------------------

def test_the_no_match_level_names_the_clearance_and_enrollment_rules():
    assert jev_score.FIT_LEVELS[0] == (
        "No match: the job is in another field (sales, recruiting, hardware, electrical, "
        "embedded or firmware work, or other work with no data, analysis or software in it), "
        "or it has a hard requirement the candidate cannot meet, such as a required master's "
        "degree or PhD, a security clearance the candidate does not hold, or an enrollment "
        "or graduation-date rule that excludes the candidate (see `candidate`).")


def test_the_clearance_and_not_eligible_options_read_as_the_brief_words_them():
    options = jev_score.MAIN_FACTOR_OPTIONS
    assert options["clearance"] == "The job requires a security clearance the candidate does not hold."
    assert options["not_eligible"] == (
        "An enrollment or graduation-date rule in the job excludes the candidate (see "
        "`candidate`): the job is only for current students when the candidate has finished "
        "school, only for a different degree level, or only for a graduation window the "
        "candidate's date falls outside.")
    assert list(options)[-1] == "not_eligible"
    assert list(options)[:-1] == [
        "skills_fit", "partial_skills", "years_1_2", "years_3_plus", "senior",
        "different_field", "degree", "clearance"]
    assert list(jev_score.stage1_questions()["main_factor"]["criteria"]) == list(options)


def test_the_factor_text_covers_the_new_wording():
    text = jev_score.FACTOR_TEXT
    assert text["clearance"] == "requires a security clearance the candidate does not hold"
    assert text["not_eligible"] == "an enrollment or graduation-date rule excludes the candidate"
    assert set(text) == set(jev_score.MAIN_FACTOR_OPTIONS)


def test_the_not_eligible_cap_is_one_beside_the_other_caps():
    assert jev_score.NOT_ELIGIBLE_CAP == 1
    assert jev_score.CLEARANCE_CAP == 1


# --- compose_stage1: the not-eligible cap -------------------------------------------------

NOT_ELIGIBLE_REASON = ("No match: an enrollment or graduation-date rule in the posting "
                       "excludes the candidate.")


@pytest.mark.parametrize("facts", [
    dict(NO_FACTS),                                # no student_cue key at all
    dict(NO_FACTS, student_cue=True),
])
def test_not_eligible_caps_the_score_at_one_when_the_posting_has_a_student_cue_or_none_is_given(facts):
    got, reason = jev_score.compose_stage1(facts, _reads(fit=1.0, main_factor="not_eligible"))
    assert got == 1
    assert reason == NOT_ELIGIBLE_REASON


def test_not_eligible_does_not_cap_when_the_posting_shows_no_student_cue():
    facts = dict(NO_FACTS, student_cue=False)
    got, reason = jev_score.compose_stage1(facts, _reads(fit=1.0, main_factor="not_eligible"))
    assert got == 5
    assert reason == "Strong match: an enrollment or graduation-date rule excludes the candidate."


def test_a_student_cue_alone_caps_nothing():
    facts = dict(NO_FACTS, student_cue=True)
    got, reason = jev_score.compose_stage1(facts, _reads(fit=1.0, main_factor="skills_fit"))
    assert got == 5
    assert reason == "Strong match: no experience bar, and the skills, tools and field line up."


def test_the_not_eligible_cap_never_raises_the_score():
    facts = dict(NO_FACTS, student_cue=True)
    got, reason = jev_score.compose_stage1(facts, _reads(fit=0.0, main_factor="not_eligible"))
    assert got == 1
    assert reason == "No match: an enrollment or graduation-date rule excludes the candidate."


def test_the_clearance_cap_names_a_clearance_the_candidate_does_not_hold():
    got, reason = jev_score.compose_stage1(dict(NO_FACTS, clearance=True), _reads(fit=1.0))
    assert got == 1
    assert reason == "No match: the posting requires a security clearance the candidate does not hold."


def test_the_not_eligible_cap_wins_over_the_degree_cap():
    facts = dict(NO_FACTS, student_cue=True, advanced_degree=True)
    got, reason = jev_score.compose_stage1(facts, _reads(fit=1.0, main_factor="not_eligible"))
    assert (got, reason) == (1, NOT_ELIGIBLE_REASON)


# --- fitted_state, stage1, stage2 -----------------------------------------------------------

CAND = jev_score.candidate_for(_profile("grad", "December 2027", "Secret", True))


def test_fitted_state_puts_the_given_candidate_into_the_state():
    questions = jev_score.stage1_questions()
    state = jev_score.fitted_state(JOB_MD, RESUME, questions, CAND)
    assert state == {"candidate": CAND, "resume": RESUME, "job": JOB_MD}
    assert jev_score.fitted_state(JOB_MD, RESUME, questions)["candidate"] == jev_score.CANDIDATE
    assert jev_score.fitted_state(JOB_MD, RESUME, questions, None)["candidate"] == jev_score.CANDIDATE


def test_fitted_state_keeps_the_given_candidate_when_it_trims_the_job(monkeypatch):
    monkeypatch.setattr(jev, "request_fits", lambda state, questions: len(state["job"]) <= 5_000)
    job = "word " * 4_000
    state = jev_score.fitted_state(job, RESUME, jev_score.stage1_questions(), CAND)
    assert len(state["job"]) <= 5_000
    assert state["candidate"] == CAND


def test_job_profile_reads_the_profile_key_of_a_mapping_job():
    profile = _profile("undergrad", "May 2027")
    assert jev_score._job_profile({"md": JOB_MD, "profile": profile}) == profile
    assert jev_score._job_profile({"md": JOB_MD}) is None
    assert jev_score._job_profile({"md": JOB_MD, "profile": "grad"}) is None
    assert jev_score._job_profile({"md": JOB_MD, "profile": None}) is None
    assert jev_score._job_profile(JOB_MD) is None
    assert jev_score._job_profile(None) is None


def test_stage1_asks_about_the_candidate_the_job_carries():
    profile = _profile("undergrad", GRAD, "Secret", True)
    judge = ScriptedJudge({"fit": 4, "main_factor": "skills_fit"})
    assert jev_score.stage1(judge, {"md": JOB_MD, "facts": NO_FACTS, "profile": profile}, RESUME)
    state, _questions = judge.calls[0]
    assert state["candidate"] == jev_score.candidate_for(profile)
    assert state["candidate"]["status"] == "In school: an undergraduate student, expected to graduate in May 2027."
    assert state["candidate"]["clearance"] == _clearance_text("Secret", True)


def test_stage2_asks_about_the_candidate_the_job_carries():
    profile = _profile("grad", "December 2027", "Top Secret", False)
    judge = ScriptedJudge()
    assert jev_score.stage2(judge, {"md": JOB2_MD, "profile": profile}, RESUME)
    state, questions = judge.calls[0]
    assert "deep_fit" in questions
    assert state["candidate"] == jev_score.candidate_for(profile)


def test_a_job_without_a_profile_asks_about_the_default_candidate():
    """The calibration script's jobs carry only md and facts; a plain job text
    carries nothing. Both keep the default candidate."""
    for job in ({"md": JOB_MD, "facts": NO_FACTS}, JOB_MD):
        judge = ScriptedJudge()
        assert jev_score.stage1(judge, job, RESUME)
        assert judge.calls[0][0]["candidate"] == jev_score.CANDIDATE
    for job in ({"md": JOB2_MD}, JOB2_MD):
        judge = ScriptedJudge()
        assert jev_score.stage2(judge, job, RESUME)
        assert judge.calls[0][0]["candidate"] == jev_score.CANDIDATE


# --- jev_facts and run_scoring -------------------------------------------------------------

SECRET_JOB = "Data analyst intern.\nRequires an active Secret clearance.\n"


def test_jev_facts_with_a_held_secret_profile_does_not_flag_a_secret_posting():
    held = sj.candidate_profile(clearance="Secret", today=TODAY)
    none = sj.candidate_profile(clearance="None", today=TODAY)
    assert sj.jev_facts(SECRET_JOB, held)["clearance"] is False
    assert sj.jev_facts(SECRET_JOB, none)["clearance"] is True
    assert sj.jev_facts(SECRET_JOB, held)["student_cue"] is True


def _patch_profile(monkeypatch, profile):
    """`score_jobs.candidate_profile` returns `profile` and counts its calls."""
    calls = []

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return profile
    monkeypatch.setattr(sj, "candidate_profile", resolve)
    return calls


HELD_SECRET = sj.candidate_profile(status="undergrad", graduation="May 2099",
                                   clearance="Secret", sponsorship=True, today=TODAY)


def test_run_scoring_hands_the_profile_to_both_stages(monkeypatch):
    _patch_profile(monkeypatch, HELD_SECRET)
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    seen = {1: [], 2: []}

    def fake_stage1(judge, job, resume):
        seen[1].append(job)
        return {"score": 4, "reason": "r"}

    def fake_stage2(judge, job, resume):
        seen[2].append(job)
        return None

    monkeypatch.setattr(jev_score, "stage1", fake_stage1)
    monkeypatch.setattr(jev_score, "stage2", fake_stage2)
    md = SECRET_JOB + JOB2_MD
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-A", md=md),
                               jev_run=sj.JevRun(ScriptedJudge())))
    assert len(seen[1]) == 1 and len(seen[2]) == 1
    job1, job2 = seen[1][0], seen[2][0]
    assert set(job1) == {"md", "facts", "profile"}
    assert job1["profile"] == HELD_SECRET.jev_profile()
    assert job1["profile"] == {"status": "undergrad", "graduation": "May 2099",
                               "clearance": "Secret", "sponsorship": True}
    assert job1["facts"] == sj.jev_facts(job1["md"], HELD_SECRET)
    assert job1["facts"]["clearance"] is False       # a held Secret meets the posting
    assert set(job2) == {"md", "profile"}
    assert job2["profile"] == HELD_SECRET.jev_profile()


def test_run_scoring_sends_jev_the_candidate_text_for_the_profile(monkeypatch):
    _patch_profile(monkeypatch, HELD_SECRET)
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    judge = ScriptedJudge()
    pool = RecordingPool()
    asyncio.run(sj.run_scoring(pool, RESUME, _jobs_df("JOB-A", "JOB-B"),
                               jev_run=sj.JevRun(judge)))
    assert pool.calls == []
    want = jev_score.candidate_for(HELD_SECRET.jev_profile())
    stage1_states = [s for s, q in judge.calls if "fit" in q]
    stage2_states = [s for s, q in judge.calls if "deep_fit" in q]
    assert len(stage1_states) == 2 and len(stage2_states) == 2
    for state in stage1_states + stage2_states:
        assert state["candidate"] == want
    assert want["status"] == "In school: an undergraduate student, expected to graduate in May 2099."


def test_run_scoring_resolves_the_profile_once_per_run(monkeypatch):
    calls = _patch_profile(monkeypatch, HELD_SECRET)
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-A", "JOB-B", "JOB-C"),
                               jev_run=sj.JevRun(ScriptedJudge())))
    assert len(calls) == 1


def test_run_scoring_keeps_the_default_candidate_when_no_setting_is_changed(monkeypatch):
    """The sandboxed settings hold the defaults, so an untouched run asks Jev
    about the default candidate."""
    monkeypatch.setattr(sj, "JEV_WRITER", False)
    judge = ScriptedJudge()
    asyncio.run(sj.run_scoring(RecordingPool(), RESUME, _jobs_df("JOB-A"),
                               jev_run=sj.JevRun(judge)))
    assert judge.calls
    for state, _questions in judge.calls:
        assert state["candidate"] == jev_score.CANDIDATE
