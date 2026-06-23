"""Row coloring (local/ui.py).

High Score / All Jobs: a job with a tailored résumé recorded gets the blue
`has_resume` tag (wins over the recommendation color) so the user can see at a
glance that a résumé already exists. Tracker: `applied` rows are blue, `rejected`
rows are red.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")
import pandas as pd  # noqa: E402

import ui  # noqa: E402


def test_has_resume_tag_wins_over_recommendation(root):
    tv = ui.make_treeview(tk.Frame(root), [("job_title", 100), ("company_name", 100)])
    df = pd.DataFrame([
        {"job_posting_id": "1", "job_title": "A", "company_name": "X", "recommendation": "apply"},
        {"job_posting_id": "2", "job_title": "B", "company_name": "Y", "recommendation": "apply"},
    ])
    ui.populate(tv, df, ["job_title", "company_name"], resume_ids={"1"})
    assert tv.item("1", "tags")[0] == "has_resume"   # tailored -> blue, wins
    assert tv.item("2", "tags")[0] == "apply"        # untailored -> reco color


def test_populate_without_resume_ids_keeps_recommendation_tag(root):
    tv = ui.make_treeview(tk.Frame(root), [("job_title", 100), ("company_name", 100)])
    df = pd.DataFrame([
        {"job_posting_id": "9", "job_title": "A", "company_name": "X", "recommendation": "consider"},
    ])
    ui.populate(tv, df, ["job_title", "company_name"])
    assert tv.item("9", "tags")[0] == "consider"


def test_tracker_status_colors_blue_and_red():
    assert ui.TAG_STYLES["applied"]["background"] == ui.BLUE_ROW_BG
    assert ui.TAG_STYLES["rejected"]["background"] == ui.RED_ROW_BG
    assert ui.TAG_STYLES["has_resume"]["background"] == ui.BLUE_ROW_BG
