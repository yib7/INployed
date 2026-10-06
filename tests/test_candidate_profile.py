"""The candidate's school status and clearance, resolved.

score_jobs.load_scoring_config() reads four keys (education_status,
graduation_month, clearance_level, clearance_sponsorship) and candidate_profile()
turns them into one CandidateProfile the prompts, the filters and the Jev scorer
read. The resolver is pure: every input is an argument that defaults to the module
constant read at call time, and `today` is injectable.

Every test runs against the sandboxed scoring constants that conftest's
_hermetic_repo_data rebinds, so the author's scoring_config.json never leaks in.
"""
import asyncio
import dataclasses
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import score_jobs as sj  # noqa: E402

TODAY = date(2026, 9, 29)

FOUR_ENV = ("SCORE_EDUCATION_STATUS", "SCORE_GRADUATION_MONTH",
            "SCORE_CLEARANCE_LEVEL", "SCORE_CLEARANCE_SPONSORSHIP")


def _clear_env(monkeypatch):
    for k in FOUR_ENV:
        monkeypatch.delenv(k, raising=False)


# --- the config keys ------------------------------------------------------------

def test_the_four_keys_carry_the_documented_env_names_and_defaults():
    d = sj._SCORING_DEFAULTS
    assert d["education_status"] == ("SCORE_EDUCATION_STATUS", "Finished school", "str")
    assert d["graduation_month"] == ("SCORE_GRADUATION_MONTH", "May 2026", "str")
    assert d["clearance_level"] == ("SCORE_CLEARANCE_LEVEL", "None", "str")
    assert d["clearance_sponsorship"] == ("SCORE_CLEARANCE_SPONSORSHIP", False, "bool")


def test_the_module_constants_hold_the_defaults_in_the_sandbox():
    assert sj.EDUCATION_STATUS == "Finished school"
    assert sj.GRADUATION_MONTH == "May 2026"
    assert sj.CLEARANCE_LEVEL == "None"
    assert sj.CLEARANCE_SPONSORSHIP is False


def test_absent_file_yields_the_four_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    cfg = sj.load_scoring_config()
    assert cfg["education_status"] == "Finished school"
    assert cfg["graduation_month"] == "May 2026"
    assert cfg["clearance_level"] == "None"
    assert cfg["clearance_sponsorship"] is False


def test_the_file_beats_the_default_for_the_four_keys(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scoring_config.json").write_text(json.dumps({
        "education_status": "In school: graduate",
        "graduation_month": "December 2027",
        "clearance_level": "Secret",
        "clearance_sponsorship": True,
    }), encoding="utf-8")
    cfg = sj.load_scoring_config()
    assert cfg["education_status"] == "In school: graduate"
    assert cfg["graduation_month"] == "December 2027"
    assert cfg["clearance_level"] == "Secret"
    assert cfg["clearance_sponsorship"] is True


def test_env_beats_the_file_for_the_four_keys(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scoring_config.json").write_text(json.dumps({
        "education_status": "In school: graduate",
        "graduation_month": "December 2027",
        "clearance_level": "Secret",
        "clearance_sponsorship": True,
    }), encoding="utf-8")
    monkeypatch.setenv("SCORE_EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setenv("SCORE_GRADUATION_MONTH", "May 2028")
    monkeypatch.setenv("SCORE_CLEARANCE_LEVEL", "TS/SCI")
    monkeypatch.setenv("SCORE_CLEARANCE_SPONSORSHIP", "0")     # "0" must read as False
    cfg = sj.load_scoring_config()
    assert cfg["education_status"] == "In school: undergraduate"
    assert cfg["graduation_month"] == "May 2028"
    assert cfg["clearance_level"] == "TS/SCI"
    assert cfg["clearance_sponsorship"] is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_the_sponsorship_env_string_reads_as_true(monkeypatch, tmp_path, raw):
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_CLEARANCE_SPONSORSHIP", raw)
    assert sj.load_scoring_config()["clearance_sponsorship"] is True


def test_a_null_graduation_month_means_no_date_and_an_absent_key_means_may_2026(
        monkeypatch, tmp_path):
    """The loader leaves a "str" value alone, so a null reaches candidate_profile()
    and reads as blank: no graduation date. A key that is absent from the file keeps
    the default, "May 2026"."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scoring_config.json").write_text(json.dumps({
        "education_status": None, "graduation_month": None, "clearance_level": None,
    }), encoding="utf-8")
    cfg = sj.load_scoring_config()
    monkeypatch.setattr(sj, "EDUCATION_STATUS", cfg["education_status"])
    monkeypatch.setattr(sj, "GRADUATION_MONTH", cfg["graduation_month"])
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", cfg["clearance_level"])
    p = sj.candidate_profile(today=TODAY)
    assert p.status == "finished"
    assert p.clearance_rank == 0 and p.clearance_label == "None"
    assert p.graduation is None and p.graduation_text is None

    (tmp_path / "scoring_config.json").write_text("{}", encoding="utf-8")
    cfg = sj.load_scoring_config()
    assert cfg["graduation_month"] == "May 2026"
    monkeypatch.setattr(sj, "GRADUATION_MONTH", cfg["graduation_month"])
    p = sj.candidate_profile(today=TODAY)
    assert p.graduation == (2026, 5) and p.graduation_text == "May 2026"


def test_conftest_sandboxes_all_four_keys():
    """A shell export or the author's scoring_config.json must never reach a test:
    conftest pops each env name and rebinds each module constant. This is a
    source-text guard against shell-export leaks: it reads conftest.py as text and
    checks that each name appears there, whatever the formatting."""
    conftest = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    for env_name in FOUR_ENV:
        assert env_name in conftest, env_name
    for attr, key in (("EDUCATION_STATUS", "education_status"),
                      ("GRADUATION_MONTH", "graduation_month"),
                      ("CLEARANCE_LEVEL", "clearance_level"),
                      ("CLEARANCE_SPONSORSHIP", "clearance_sponsorship")):
        # The bare constant name: the lookbehind skips the SCORE_ env spelling.
        assert re.search(rf"(?<![A-Z_]){attr}(?![A-Z_])", conftest), attr
        assert key in conftest, key


# --- the constants --------------------------------------------------------------

def test_the_status_and_clearance_tables():
    assert sj.EDUCATION_STATUSES == ("Finished school", "In school: undergraduate",
                                     "In school: graduate")
    assert sj.EDUCATION_STATUS_CODES == ("finished", "undergrad", "grad")
    assert sj.CLEARANCE_LEVELS == ("None", "Public Trust", "Secret", "Top Secret", "TS/SCI")


# --- parse_graduation_month -----------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("May 2026", (2026, 5)),
    ("January 2027", (2027, 1)),
    ("Jan 2027", (2027, 1)),
    ("February 2028", (2028, 2)),
    ("Feb 2028", (2028, 2)),
    ("March 2026", (2026, 3)),
    ("mar 2026", (2026, 3)),
    ("April 2026", (2026, 4)),
    ("Apr 2026", (2026, 4)),
    ("June 2026", (2026, 6)),
    ("Jun 2026", (2026, 6)),
    ("July 2026", (2026, 7)),
    ("Jul 2026", (2026, 7)),
    ("August 2026", (2026, 8)),
    ("Aug 2026", (2026, 8)),
    ("September 2026", (2026, 9)),
    ("Sept 2026", (2026, 9)),
    ("Sept. 2026", (2026, 9)),
    ("Sep 2026", (2026, 9)),
    ("October 2026", (2026, 10)),
    ("Oct 2026", (2026, 10)),
    ("November 2026", (2026, 11)),
    ("Nov 2026", (2026, 11)),
    ("December 2026", (2026, 12)),
    ("Dec 2026", (2026, 12)),
    ("dec. 2026", (2026, 12)),
    ("MAY 2026", (2026, 5)),
    ("may 2026", (2026, 5)),
    ("  May   2026  ", (2026, 5)),
    ("\tDec.\t2030\n", (2030, 12)),
    ("May 1999", (1999, 5)),
    ("\u017fep 2026", (2026, 9)),          # long s: the regex matches it, casefold maps it
])
def test_parse_graduation_month_accepts(text, expected):
    assert sj.parse_graduation_month(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, "garbage", "May", "2026", "May 26", "May 3026", "May 1899",
    "Maybe 2026", "Smarch 2026", "5/2026", "2026-05", "May, 2026", "Mayy 2026",
    "May 2026 and more", "May2026", 2026, ["May 2026"],
])
def test_parse_graduation_month_rejects(text):
    assert sj.parse_graduation_month(text) is None


def test_parse_graduation_month_is_none_when_a_match_names_no_month(monkeypatch):
    """The regex matched but the prefix lookup came up empty: None, never StopIteration."""
    monkeypatch.setattr(sj, "_MONTH_NAMES", ("Xanuary",) + sj._MONTH_NAMES[1:])
    assert sj.GRADUATION_MONTH_RE.fullmatch("Jan 2026")
    assert sj.parse_graduation_month("Jan 2026") is None
    assert sj.parse_graduation_month("Feb 2026") == (2026, 2)


# --- candidate_profile: defaults, statuses, clearances --------------------------

def test_the_defaults_resolve_to_finished_may_2026_no_clearance():
    p = sj.candidate_profile(today=TODAY)
    assert p.status == "finished"
    assert p.graduation == (2026, 5)
    assert p.graduation_text == "May 2026"
    assert p.clearance_rank == 0
    assert p.clearance_label == "None"
    assert p.sponsorship is False
    assert p.rolled_over is False
    assert p.notes == ()


@pytest.mark.parametrize("label, code", [
    ("Finished school", "finished"),
    ("In school: undergraduate", "undergrad"),
    ("In school: graduate", "grad"),
])
@pytest.mark.parametrize("variant", [
    lambda s: s, lambda s: s.lower(), lambda s: s.upper(), lambda s: f"  {s}  ",
    lambda s: s.swapcase(),
])
def test_every_status_label_resolves_in_any_case(label, code, variant):
    p = sj.candidate_profile(status=variant(label), graduation="", today=TODAY)
    assert p.status == code
    assert p.notes == ()


@pytest.mark.parametrize("rank, label", list(enumerate(
    ["None", "Public Trust", "Secret", "Top Secret", "TS/SCI"])))
@pytest.mark.parametrize("variant", [
    lambda s: s, lambda s: s.lower(), lambda s: s.upper(), lambda s: f" {s} ",
])
def test_every_clearance_label_resolves_in_any_case(rank, label, variant):
    p = sj.candidate_profile(clearance=variant(label), today=TODAY)
    assert p.clearance_rank == rank
    assert p.clearance_label == label      # canonical spelling, whatever came in
    assert p.notes == ()


@pytest.mark.parametrize("code", sj.EDUCATION_STATUS_CODES)
@pytest.mark.parametrize("variant", [
    lambda s: s, lambda s: s.upper(), lambda s: f"  {s}  ", lambda s: s.swapcase(),
])
def test_every_status_code_is_an_alias_for_its_label(code, variant):
    p = sj.candidate_profile(status=variant(code), graduation="", today=TODAY)
    assert p.status == code
    assert p.notes == ()


def test_a_status_code_and_its_label_resolve_alike():
    for code, label in zip(sj.EDUCATION_STATUS_CODES, sj.EDUCATION_STATUSES):
        by_code = sj.candidate_profile(status=code, graduation="May 2027", today=TODAY)
        by_label = sj.candidate_profile(status=label, graduation="May 2027", today=TODAY)
        assert by_code == by_label


@pytest.mark.parametrize("bad", ["Graduated", "in school", "phd", "In school: PhD", "alumnus"])
def test_an_unknown_status_falls_back_to_finished_with_a_note(bad):
    p = sj.candidate_profile(status=bad, graduation="May 2027", today=TODAY)
    assert p.status == "finished"
    assert len(p.notes) >= 1
    assert any(bad in n for n in p.notes)
    assert p.rolled_over is False


@pytest.mark.parametrize("bad", ["Confidential", "Q clearance", "TS SCI", "secret!"])
def test_an_unknown_clearance_falls_back_to_none_with_a_note(bad):
    p = sj.candidate_profile(clearance=bad, today=TODAY)
    assert p.clearance_rank == 0 and p.clearance_label == "None"
    assert len(p.notes) == 1
    assert bad in p.notes[0]


def test_a_blank_status_and_clearance_read_as_unset_without_a_note():
    p = sj.candidate_profile(status="  ", clearance="", graduation="", today=TODAY)
    assert p.status == "finished"
    assert p.clearance_rank == 0
    assert p.graduation is None and p.graduation_text is None
    assert p.notes == ()


def test_sponsorship_is_a_bool_however_it_arrives():
    assert sj.candidate_profile(sponsorship=True, today=TODAY).sponsorship is True
    assert sj.candidate_profile(sponsorship=False, today=TODAY).sponsorship is False
    assert sj.candidate_profile(sponsorship="yes", today=TODAY).sponsorship is True
    assert sj.candidate_profile(sponsorship="0", today=TODAY).sponsorship is False


# --- graduation month -----------------------------------------------------------

def test_the_graduation_text_is_the_full_month_name_and_the_year():
    p = sj.candidate_profile(status="In school: undergraduate", graduation="  sept.  2027 ",
                             today=TODAY)
    assert p.graduation == (2027, 9)
    assert p.graduation_text == "September 2027"


@pytest.mark.parametrize("bad", ["May 26", "soon", "2027", "next May"])
def test_an_unparseable_graduation_month_is_dropped_with_a_note(bad):
    p = sj.candidate_profile(status="In school: undergraduate", graduation=bad, today=TODAY)
    assert p.graduation is None and p.graduation_text is None
    assert p.status == "undergrad"          # no date means no rollover
    assert len(p.notes) == 1
    assert bad in p.notes[0]


# --- rollover -------------------------------------------------------------------

@pytest.mark.parametrize("status, code", [
    ("In school: undergraduate", "undergrad"),
    ("In school: graduate", "grad"),
])
def test_in_school_stays_through_the_last_day_of_the_graduation_month(status, code):
    p = sj.candidate_profile(status=status, graduation="May 2026", today=date(2026, 5, 31))
    assert p.status == code
    assert p.rolled_over is False
    assert p.graduation == (2026, 5)
    assert p.notes == ()


@pytest.mark.parametrize("status", ["In school: undergraduate", "In school: graduate"])
def test_in_school_rolls_over_to_finished_on_the_first_day_after_the_month(status):
    p = sj.candidate_profile(status=status, graduation="May 2026", today=date(2026, 6, 1))
    assert p.status == "finished"
    assert p.rolled_over is True
    assert p.graduation == (2026, 5)            # the date stays: "graduated May 2026"
    assert p.graduation_text == "May 2026"
    assert len(p.notes) == 1
    assert "May 2026" in p.notes[0]


def test_rollover_crosses_a_year_boundary():
    p = sj.candidate_profile(status="In school: graduate", graduation="December 2026",
                             today=date(2027, 1, 1))
    assert p.status == "finished" and p.rolled_over is True
    q = sj.candidate_profile(status="In school: graduate", graduation="December 2026",
                             today=date(2026, 12, 31))
    assert q.status == "grad" and q.rolled_over is False


def test_a_far_future_graduation_stays_in_school():
    p = sj.candidate_profile(status="In school: undergraduate", graduation="May 2030",
                             today=TODAY)
    assert p.status == "undergrad" and p.rolled_over is False and p.notes == ()


def test_finished_with_a_past_or_current_month_keeps_the_date():
    past = sj.candidate_profile(status="Finished school", graduation="May 2026", today=TODAY)
    assert past.graduation == (2026, 5) and past.graduation_text == "May 2026"
    assert past.notes == () and past.rolled_over is False
    this_month = sj.candidate_profile(status="Finished school", graduation="September 2026",
                                      today=TODAY)
    assert this_month.graduation == (2026, 9) and this_month.notes == ()


def test_finished_with_a_future_month_drops_the_date_with_a_note():
    p = sj.candidate_profile(status="Finished school", graduation="October 2026", today=TODAY)
    assert p.status == "finished"
    assert p.graduation is None and p.graduation_text is None
    assert p.rolled_over is False
    assert len(p.notes) == 1
    assert "October 2026" in p.notes[0]


def test_today_defaults_to_the_clock_read_at_call_time(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 5, 31, 23, 59)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(sj, "datetime", Clock)
    kwargs = dict(status="In school: undergraduate", graduation="May 2026")
    assert sj.candidate_profile(**kwargs).status == "undergrad"
    Clock.current = datetime(2026, 6, 1, 0, 0)
    p = sj.candidate_profile(**kwargs)
    assert p.status == "finished" and p.rolled_over is True


def test_the_module_constants_are_read_at_call_time(monkeypatch):
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: graduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "Dec 2027")
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "top secret")
    monkeypatch.setattr(sj, "CLEARANCE_SPONSORSHIP", True)
    p = sj.candidate_profile(today=TODAY)
    assert p.status == "grad"
    assert p.graduation == (2027, 12) and p.graduation_text == "December 2027"
    assert p.clearance_rank == 3 and p.clearance_label == "Top Secret"
    assert p.sponsorship is True
    assert p.rolled_over is False


def test_an_argument_beats_the_module_constant(monkeypatch):
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Secret")
    monkeypatch.setattr(sj, "CLEARANCE_SPONSORSHIP", True)
    p = sj.candidate_profile(clearance="None", sponsorship=False, today=TODAY)
    assert p.clearance_rank == 0 and p.sponsorship is False


def test_the_resolver_prints_nothing(capsys):
    sj.candidate_profile(status="bogus", graduation="soon", clearance="nope", today=TODAY)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- the dataclass --------------------------------------------------------------

def test_the_profile_is_frozen():
    p = sj.candidate_profile(today=TODAY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = "grad"
    assert isinstance(p.notes, tuple)


def test_jev_profile_is_the_four_key_mapping():
    p = sj.candidate_profile(status="In school: undergraduate", graduation="May 2027",
                             clearance="Secret", sponsorship=True, today=TODAY)
    assert p.jev_profile() == {"status": "undergrad", "graduation": "May 2027",
                               "clearance": "Secret", "sponsorship": True}
    d = sj.candidate_profile(today=TODAY).jev_profile()
    assert d == {"status": "finished", "graduation": "May 2026",
                 "clearance": "None", "sponsorship": False}


def test_jev_profile_carries_none_when_the_graduation_is_unknown():
    d = sj.candidate_profile(graduation="", today=TODAY).jev_profile()
    assert d["graduation"] is None


def test_jev_profile_reads_the_effective_status_after_a_rollover():
    p = sj.candidate_profile(status="In school: undergraduate", graduation="May 2026",
                             today=date(2026, 6, 1))
    assert p.jev_profile()["status"] == "finished"


@pytest.mark.parametrize("code, label", list(zip(sj.EDUCATION_STATUS_CODES,
                                                 sj.EDUCATION_STATUSES)))
def test_status_label_reads_the_label_for_each_code(code, label):
    p = sj.candidate_profile(status=code, graduation="", today=TODAY)
    assert p.status_label == label


def test_status_label_follows_the_effective_status_after_a_rollover():
    p = sj.candidate_profile(status="In school: graduate", graduation="May 2026",
                             today=date(2026, 6, 1))
    assert p.status == "finished"
    assert p.status_label == "Finished school"


def _hand_built(status):
    return sj.CandidateProfile(status=status, graduation=None, graduation_text=None,
                               clearance_rank=0, clearance_label="None", sponsorship=False)


@pytest.mark.parametrize("odd", ["alumnus", "", "GRAD", "In school: graduate"])
def test_status_label_is_finished_school_for_a_code_the_table_lacks(odd):
    assert _hand_built(odd).status_label == "Finished school"


def test_describe_a_hand_built_profile_with_an_odd_code_does_not_raise():
    line = sj.describe_profile(_hand_built("alumnus"))
    assert line == "Candidate: Finished school; clearance: None, not open to sponsorship"


# --- describe_profile -----------------------------------------------------------

def test_describe_the_default_profile():
    line = sj.describe_profile(sj.candidate_profile(today=TODAY))
    assert line == ("Candidate: Finished school (graduated May 2026); "
                    "clearance: None, not open to sponsorship")


def test_describe_an_in_school_profile_with_a_clearance():
    p = sj.candidate_profile(status="In school: undergraduate", graduation="May 2027",
                             clearance="Secret", sponsorship=True, today=TODAY)
    assert sj.describe_profile(p) == (
        "Candidate: In school: undergraduate (expected May 2027); "
        "clearance: Secret, open to sponsorship")


def test_describe_a_graduate_student():
    p = sj.candidate_profile(status="In school: graduate", graduation="Dec 2027",
                             clearance="TS/SCI", today=TODAY)
    assert sj.describe_profile(p) == (
        "Candidate: In school: graduate (expected December 2027); "
        "clearance: TS/SCI, not open to sponsorship")


def test_describe_a_profile_with_no_graduation_month():
    p = sj.candidate_profile(graduation="", today=TODAY)
    assert sj.describe_profile(p) == (
        "Candidate: Finished school; clearance: None, not open to sponsorship")


def test_describe_a_rolled_over_profile_says_finished_and_carries_the_note():
    p = sj.candidate_profile(status="In school: graduate", graduation="May 2026",
                             today=date(2026, 6, 1))
    text = sj.describe_profile(p)
    lines = text.splitlines()
    assert lines[0] == ("Candidate: Finished school (graduated May 2026); "
                        "clearance: None, not open to sponsorship")
    assert len(lines) == 1 + len(p.notes)
    assert all(n in text for n in p.notes)


def test_describe_appends_every_note_on_its_own_line():
    p = sj.candidate_profile(status="bogus", clearance="nope", graduation="soon", today=TODAY)
    assert len(p.notes) == 3
    lines = sj.describe_profile(p).splitlines()
    assert lines[0].startswith("Candidate: Finished school")
    assert len(lines) == 4
    for note, line in zip(p.notes, lines[1:]):
        assert note in line


def test_the_notes_and_the_run_line_obey_the_writing_rules():
    em_dash = chr(0x2014)
    profiles = [
        sj.candidate_profile(status="bogus", clearance="nope", graduation="soon", today=TODAY),
        sj.candidate_profile(status="In school: graduate", graduation="May 2026",
                             today=date(2026, 6, 1)),
        sj.candidate_profile(status="Finished school", graduation="Oct 2026", today=TODAY),
    ]
    banned = (em_dash, " -- ", ", not", "rather than", "instead of", "not just")
    for p in profiles:
        for text in p.notes:
            assert not any(b in text for b in banned), text
        # The run line's own ", not open to sponsorship" is the specified wording.
        line = sj.describe_profile(p)
        assert em_dash not in line and " -- " not in line


# --- main() ---------------------------------------------------------------------

def _stub_main(monkeypatch, **args):
    """Everything main() touches before the heal step, faked."""

    class Pool:
        def stats(self):
            return {}

    async def rescore(pool, resume, *, jev_run=None):
        return 0, 0

    def make_pool(required=True):
        return Pool()

    ns = {"csv": None, "heal_reused": False, "dry_run": False}
    ns.update(args)
    monkeypatch.setattr(sj, "parse_args", lambda: SimpleNamespace(**ns))
    monkeypatch.setattr(sj, "load_resume", lambda: "resume")
    monkeypatch.setattr(sj, "latest_input_csv", lambda: None)
    monkeypatch.setattr(sj, "make_jev_judge", lambda: None)
    monkeypatch.setattr(sj, "make_pool", make_pool)
    monkeypatch.setattr(sj, "heal_master_reuse", lambda dry_run=False: 0)
    monkeypatch.setattr(sj, "rescore_master_failures", rescore)
    monkeypatch.setattr(sj, "append_run_stats", lambda stats: None)


def test_main_prints_the_candidate_line_once_per_run(monkeypatch, capsys):
    _stub_main(monkeypatch)
    asyncio.run(sj.main())
    out = capsys.readouterr().out
    assert out.count("Candidate: ") == 1
    assert ("Candidate: Finished school (graduated May 2026); "
            "clearance: None, not open to sponsorship") in out


def test_main_prints_the_configured_candidate_and_its_notes(monkeypatch, capsys):
    _stub_main(monkeypatch)
    monkeypatch.setattr(sj, "EDUCATION_STATUS", "In school: undergraduate")
    monkeypatch.setattr(sj, "GRADUATION_MONTH", "May 2099")
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Confidential")     # unknown: yields a note
    monkeypatch.setattr(sj, "CLEARANCE_SPONSORSHIP", True)
    asyncio.run(sj.main())
    out = capsys.readouterr().out
    assert out.count("Candidate: ") == 1
    assert ("Candidate: In school: undergraduate (expected May 2099); "
            "clearance: None, open to sponsorship") in out
    assert "  Note: Unknown clearance level 'Confidential'; using None." in out.splitlines()


def test_main_prints_the_note_for_a_bad_setting(monkeypatch, capsys):
    _stub_main(monkeypatch)
    monkeypatch.setattr(sj, "CLEARANCE_LEVEL", "Confidential")
    asyncio.run(sj.main())
    out = capsys.readouterr().out
    assert out.count("Candidate: ") == 1
    assert "Confidential" in out


def test_the_heal_one_shot_does_not_print_the_candidate_line(monkeypatch, capsys):
    """--heal-reused repairs the master and scores nothing."""
    _stub_main(monkeypatch, heal_reused=True)
    monkeypatch.setattr(sj, "run_heal_reused", lambda dry_run=False: None)
    asyncio.run(sj.main())
    assert "Candidate: " not in capsys.readouterr().out
