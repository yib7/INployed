"""Jobs outside the United States are filtered before scoring and hidden from
the High Score list.

The scrape asks Bright Data for United States jobs, but some runs return whole
inputs with a worldwide mix (2026-10-06: Jakarta, London, Singapore, Nenzing
scored apply). The location strings below are real LinkedIn shapes from the
master.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402
import score_jobs as sj  # noqa: E402
from us_location import is_non_us_location  # noqa: E402

US = [
    "New York, NY", "Phoenix, AZ", "United States", "California, United States",
    "Austin, Texas Metropolitan Area", "Greater Birmingham, Alabama Area",
    "New York City Metropolitan Area", "San Francisco Bay Area",
    "Washington DC-Baltimore Area", "Dallas-Fort Worth Metroplex", "Greater Boston",
    "Charlotte Metro", "Utica-Rome Area", "Georgia", "San Juan, Puerto Rico",
    "Paris, TX", "Dublin, OH", "Melbourne, FL", "", None, float("nan"),
]
ABROAD = [
    "West Jakarta, Jakarta, Indonesia", "London Area, United Kingdom",
    "Nenzing, Vorarlberg, Austria", "Singapore, Singapore",
    "Bengaluru East, Karnataka, India", "Hong Kong, Hong Kong SAR", "Toronto, ON",
    "Brazil", "Canada", "Türkiye", "European Union", "Greater Bengaluru Area",
    "Pune/Pimpri-Chinchwad Area", "Metro Manila", "Greater Paris Metropolitan Region",
    "Mexico City Metropolitan Area", "Cracow Metropolitan Area", "Lisbon Metropolitan Area",
]


@pytest.mark.parametrize("location", US)
def test_a_us_location_is_kept(location):
    assert is_non_us_location(location) is False


@pytest.mark.parametrize("location", ABROAD)
def test_a_location_abroad_is_flagged(location):
    assert is_non_us_location(location) is True


CLEAN_DESC = "We are hiring a backend software engineer to build web apps and REST APIs."


def _frame(locations):
    return pd.DataFrame({"desc": [CLEAN_DESC] * len(locations), "title": ["Data Engineer"] * len(locations),
                         "job_location": locations})


def test_the_scorer_filters_a_job_abroad_before_scoring():
    out = sj.add_filter_columns(_frame(["Frisco, TX", "Singapore, Singapore"]), "desc", "title")
    assert list(out["filter_non_us"]) == [False, True]
    assert list(out["filtered_out"]) == [False, True]
    assert out["filter_non_us"].dtype == bool


def test_without_a_location_column_the_filter_is_all_false():
    df = pd.DataFrame({"desc": [CLEAN_DESC], "title": ["Data Engineer"]})
    out = sj.add_filter_columns(df, "desc", "title")
    assert list(out["filter_non_us"]) == [False]


def test_a_vm_copy_without_us_location_warns_once_and_keeps_scoring(monkeypatch, capsys):
    monkeypatch.setattr(sj, "is_non_us_location", None)
    monkeypatch.setattr(sj, "_warned_no_us_location", False)
    out = sj.add_filter_columns(_frame(["Singapore, Singapore"]), "desc", "title")
    sj.add_filter_columns(_frame(["Singapore, Singapore"]), "desc", "title")
    assert list(out["filter_non_us"]) == [False]
    assert capsys.readouterr().out.count("us_location.py is not beside") == 1


def test_score_cols_fold_filter_non_us_into_the_master():
    assert sj.SCORE_COLS.count("filter_non_us") == 1


def test_the_high_score_list_hides_a_job_abroad_but_not_a_hand_added_one():
    df = pd.DataFrame([
        {"job_posting_id": "1", "job_location": "Frisco, TX", "score": "4", "is_seen": "no"},
        {"job_posting_id": "2", "job_location": "West Jakarta, Jakarta, Indonesia",
         "score": "4", "is_seen": "no"},
        {"job_posting_id": "manual-abc", "job_location": "London, United Kingdom",
         "score": "", "is_seen": "no"},
    ])
    out = jobsdata.filter_high_unseen(df, 4)
    assert sorted(out["job_posting_id"]) == ["1", "manual-abc"]


def test_the_high_score_list_works_without_a_location_column():
    df = pd.DataFrame([{"job_posting_id": "1", "score": "5", "is_seen": "no"}])
    assert list(jobsdata.filter_high_unseen(df, 4)["job_posting_id"]) == ["1"]
