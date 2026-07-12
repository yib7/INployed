"""Cycle 40 Phase 3b/3c: jobsdata.job_detail_fields + the JobDetailCard."""
import pandas as pd

import jobsdata
from qt.detail_card import JobDetailCard


def _row(**over):
    base = {
        "job_posting_id": "1", "job_title": "AI Engineer",
        "company_name": "Riverstone", "job_location": "Boston, MA",
        "url": "https://x/1", "score": "5", "deep_score": "8.5",
        "recommendation": "apply", "applicants": "4",
        "job_base_pay_range": "$105k–$135k", "job_posted_date": "2026-07-11T00:00",
        "reason": "great fit", "strengths": "LLM pipeline|Python depth",
        "gaps": "No fintech", "job_summary": "A summary long enough to be used "
        "directly as the JD snippet for the detail card.",
    }
    base.update(over)
    return pd.Series(base)


# ---- job_detail_fields ------------------------------------------------------------

def test_job_detail_fields_structured_output():
    f = jobsdata.job_detail_fields(_row())
    assert f["title"] == "AI Engineer" and f["company"] == "Riverstone"
    assert f["score"] == "5" and f["deep_score"] == "8.5"
    assert f["strengths"] == ["LLM pipeline", "Python depth"]
    assert f["gaps"] == ["No fintech"]
    assert f["posted"] == "2026-07-11"           # date-only, like the segments
    assert f["salary"] == "$105k–$135k"
    assert f["jd"].startswith("A summary")
    assert f["snapshot_only"] is False


def test_job_detail_fields_snapshot_only_and_empty():
    assert jobsdata.job_detail_fields(None) == {}
    f = jobsdata.job_detail_fields(None, {"job_title": "Old Role",
                                          "company": "GoneCo", "url": "https://x"})
    assert f["snapshot_only"] is True
    assert f["title"] == "Old Role" and f["company"] == "GoneCo"
    assert "tracker snapshot" in f["note"]


def test_job_detail_fields_segments_stay_untouched():
    # The card's dict feed rides ALONGSIDE job_detail_segments (test-coupled).
    segs = jobsdata.job_detail_segments(_row())
    assert any("Riverstone" in t for t, _s in segs)


# ---- JobDetailCard ------------------------------------------------------------------

def test_card_renders_fields_to_plain_text(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    text = card.toPlainText()
    for expected in ("AI Engineer", "Riverstone", "great fit",
                     "LLM pipeline", "No fintech", "https://x/1"):
        assert expected in text
    assert not card._empty.isVisible() or card._empty.isHidden()


def test_card_empty_state(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    assert card.toPlainText() == ""
    assert card._content.isHidden()
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert not card._content.isHidden()
    card.set_empty()
    assert card.toPlainText() == "" and card._content.isHidden()


def test_card_description_collapsed_behind_toggle(qtbot):
    # Locked user decision: the JD snippet stays, collapsed by default.
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert card.desc_label.isHidden()
    assert card.desc_toggle.text() == "Show description"
    card.desc_toggle.setChecked(True)
    assert not card.desc_label.isHidden()
    assert card.desc_toggle.text() == "Hide description"
    # a new selection re-collapses it
    card.set_fields(jobsdata.job_detail_fields(_row(job_posting_id="2")), jid="2")
    assert card.desc_label.isHidden()


def test_card_buttons_fire_callbacks(qtbot):
    fired = []
    card = JobDetailCard(on_open=lambda jid: fired.append(("open", jid)),
                         on_tailor=lambda: fired.append(("tailor",)),
                         on_apply=lambda: fired.append(("apply",)))
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    card.open_btn.click()
    card.tailor_btn.click()
    card.apply_btn.click()
    assert fired == [("open", "1"), ("tailor",), ("apply",)]


def test_card_tracker_variant_swaps_actions_and_lede(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    tracker = {"status": "applied", "applied_date": "2026-07-04", "days": "8",
               "follow_up": "DUE", "next_step": "No reply in 8 days — follow up."}
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1", tracker=tracker)
    assert card.tailor_btn.isHidden() and card.apply_btn.isHidden()
    assert not card.resume_btn.isHidden() and not card.followup_btn.isHidden()
    text = card.toPlainText()
    assert "NEXT STEP" in text and "follow up" in text.lower()
    assert "8 days since applying" in text
    # discovery mode restores the Tailor/Apply pair
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert not card.tailor_btn.isHidden() and not card.apply_btn.isHidden()
    assert card.resume_btn.isHidden() and card.followup_btn.isHidden()
