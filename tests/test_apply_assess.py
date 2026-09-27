"""The auto-apply difficulty check (cycle 19, SP6: DF-1 to DF-6).

DF-3's score is pure code over counts the check reads from the first
application page: a base by application system plus fixed steps, rounded half
up and clamped to 1-10, with Easy Apply, a closed or dead posting and a payment
page at 10 at once.
"""
from __future__ import annotations

import pytest

import apply_assess as aa

# --- DF-3: the score ---------------------------------------------------------------------


@pytest.mark.parametrize("system, base", [
    ("greenhouse", 2), ("lever", 2), ("ashby", 2),
    ("workable", 3), ("smartrecruiters", 3), ("jobvite", 3), ("bamboohr", 3),
    ("workday", 6), ("icims", 6),
    ("taleo", 7), ("successfactors", 7), ("oracle", 7),
    ("other", 4), ("", 4), ("brassring", 4), ("adp", 4),
])
def test_the_base_follows_the_application_system(system, base):
    assert aa.score(system=system)["score"] == base


def test_the_constants_are_the_specs():
    assert aa.SYSTEM_BASE == {
        "greenhouse": 2, "lever": 2, "ashby": 2,
        "workable": 3, "smartrecruiters": 3, "jobvite": 3, "bamboohr": 3,
        "workday": 6, "icims": 6,
        "taleo": 7, "successfactors": 7, "oracle": 7,
    }
    assert aa.UNKNOWN_BASE == 4
    assert (aa.PER_UNANSWERED, aa.UNANSWERED_CAP) == (1.5, 5)
    assert (aa.PER_ESSAY, aa.ESSAY_CAP) == (0.5, 2)
    assert (aa.SENSITIVE, aa.CAPTCHA, aa.ACCOUNT_WALL) == (3, 2, 1)
    assert (aa.HISTORY_EASIER, aa.HISTORY_HARDER, aa.HISTORY_MIN) == (-1, 1, 2)
    assert (aa.SCORE_MIN, aa.SCORE_MAX) == (1, 10)


def test_each_unanswerable_required_question_adds_one_and_a_half_up_to_five():
    got = [aa.score(system="greenhouse", unanswered=n)["score"] for n in range(6)]
    assert got == [2, 4, 5, 7, 7, 7]      # 2, 3.5, 5, 6.5, then 2 + 5


def test_each_required_essay_adds_a_half_up_to_two():
    got = [aa.score(system="greenhouse", essays=n)["score"] for n in range(6)]
    assert got == [2, 3, 3, 4, 4, 4]      # 2, 2.5, 3, 3.5, then 2 + 2


def test_a_half_rounds_up():
    assert aa.score(system="workable", essays=1)["score"] == 4          # 3.5
    assert aa.score(system="greenhouse", essays=1)["score"] == 3        # 2.5
    assert aa.score(system="workday", unanswered=1)["score"] == 8       # 7.5


def test_a_sensitive_field_a_captcha_and_an_account_wall_add_their_steps():
    assert aa.score(system="greenhouse", sensitive=True)["score"] == 5
    assert aa.score(system="greenhouse", captcha=True)["score"] == 4
    assert aa.score(system="greenhouse", account_wall=True)["score"] == 3
    assert aa.score(system="greenhouse", sensitive=True, captcha=True,
                    account_wall=True)["score"] == 8


def test_past_drains_move_the_score_by_one():
    assert aa.score(system="workday", past_submits=2)["score"] == 5
    assert aa.score(system="workday", past_submits=1)["score"] == 6
    assert aa.score(system="workday", past_parks=2)["score"] == 7
    assert aa.score(system="workday", past_parks=1)["score"] == 6


def test_one_park_takes_away_the_easier_step_and_two_parks_win():
    """"Submitted at least twice without a park": one park on that system
    keeps the easier step off, and two parks add the harder step."""
    assert aa.score(system="workday", past_submits=5, past_parks=1)["score"] == 6
    assert aa.score(system="workday", past_submits=5, past_parks=2)["score"] == 7


def test_the_score_is_clamped_to_one_through_ten():
    most = aa.score(system="oracle", unanswered=9, essays=9, sensitive=True, captcha=True,
                    account_wall=True, past_parks=3)
    assert most["score"] == 10
    least = aa.score(system="greenhouse", past_submits=9)
    assert least["score"] == 1


@pytest.mark.parametrize("stop", ["easy_apply", "closed", "dead", "payment"])
def test_easy_apply_a_closed_or_dead_posting_and_payment_are_ten_at_once(stop):
    got = aa.score(system="greenhouse", stop=stop)
    assert got["score"] == 10
    assert got["band"] == "Do it yourself"
    assert got["reasons"] == [aa.STOP_REASONS[stop]]


def test_a_stop_ignores_every_other_count():
    got = aa.score(system="greenhouse", stop="payment", past_submits=9, unanswered=0)
    assert got["score"] == 10


def test_an_unknown_stop_is_a_bug_and_raises():
    with pytest.raises(ValueError):
        aa.score(system="greenhouse", stop="gone")


@pytest.mark.parametrize("value, band", [
    (1, "Queue it"), (2, "Queue it"), (3, "Queue it"),
    (4, "May need an answer or two"), (5, "May need an answer or two"),
    (6, "May need an answer or two"),
    (7, "Do it yourself"), (8, "Do it yourself"), (9, "Do it yourself"),
    (10, "Do it yourself"),
])
def test_the_bands(value, band):
    assert aa.band_for(value) == band


def test_the_score_carries_its_band():
    assert aa.score(system="greenhouse")["band"] == "Queue it"
    assert aa.score(system="workday")["band"] == "May need an answer or two"
    assert aa.score(system="taleo")["band"] == "Do it yourself"


def test_the_reasons_name_each_step_the_score_took():
    got = aa.score(system="workday", unanswered=2, essays=1, sensitive=True, captcha=True,
                   account_wall=True, past_parks=2)
    assert got["reasons"] == [
        "Application system: Workday (base 6)",
        "2 required questions your answers cannot fill (+3)",
        "1 required essay the run would draft (+0.5)",
        "A required sensitive field, always yours to type (+3)",
        "A CAPTCHA or bot check (+2)",
        "An account wall with no saved account (+1)",
        "Past runs on Workday parked at least twice (+1)",
    ]
    assert got["score"] == 10


def test_the_reasons_for_an_unknown_system_and_a_smooth_history():
    got = aa.score(system="other", unanswered=1, past_submits=3)
    assert got["reasons"] == [
        "Application system: not one the check knows (base 4)",
        "1 required question your answers cannot fill (+1.5)",
        "Past runs on this system reached the end at least twice with no park (-1)",
    ]
    assert got["score"] == 5       # 4 + 1.5 - 1 = 4.5, rounded up


def test_the_capped_steps_say_so():
    got = aa.score(system="lever", unanswered=5, essays=6)
    assert got["reasons"][1:] == [
        "5 required questions your answers cannot fill (+5, the most this step adds)",
        "6 required essays the run would draft (+2, the most this step adds)",
    ]
