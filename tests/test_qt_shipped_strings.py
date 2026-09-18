"""The em-dash ban covers every string a user reads off the running dashboard.

tests/test_settings.py pins the Settings labels and help strings; this module pins
the rest of the primary journey, the strings that appear in the README stills and
the demo GIF: the freshness pill, the Settings search placeholder, every section
tagline and description, the High Score and Tracker legends, the Tracker's NEXT
STEP line, and the Resume Data staleness banner. A colon, a semicolon, a period
or a parenthesis does the same job in each of them.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

from PySide6 import QtWidgets

from qt import jobs_tab
from qt import settings_tab as st
from qt.main_window import MainWindow
from qt.resume_data_tab import ResumeDataEditor
from qt.settings_tab import SettingsForm
from qt.stats_tab import StatsTab

DASH = chr(0x2014)          # the em dash
DOT = chr(0x25CF)            # the badge bullet


def test_settings_section_copy_carries_no_em_dash():
    offenders = {name: text for table in (st.SECTION_TAGLINE, st.SECTION_HELP)
                 for name, text in table.items() if DASH in text}
    assert offenders == {}, f"use ':' or ';' in the section copy: {offenders}"


def test_settings_search_placeholder_carries_no_em_dash(qtbot, tmp_path):
    form = SettingsForm(targets={"env": tmp_path / ".env", "config": tmp_path / "c.json",
                                 "search": tmp_path / "s.json", "scoring": tmp_path / "sc.json"},
                        collapsed_sections=[], save_collapsed=lambda s: None,
                        show_advanced=False, save_show_advanced=lambda v: None)
    qtbot.addWidget(form)
    boxes = [b for b in form.findChildren(QtWidgets.QLineEdit)
             if b.accessibleName() == "Search settings"]
    assert len(boxes) == 1
    assert boxes[0].placeholderText().startswith("Search settings:")
    assert DASH not in boxes[0].placeholderText()


def test_legend_labels_carry_no_em_dash():
    for key in ("high", "tracker"):        # All Jobs is untinted and has no legend
        for _color, label in jobs_tab.legend_items_for(key):
            assert DASH not in label, (key, label)
    assert any(label == "Tailored: resume ready" for _c, label in jobs_tab.legend_items_for("high"))


def test_freshness_badge_carries_no_em_dash(qtbot):
    tab = StatsTab()
    qtbot.addWidget(tab)
    tab.set_freshness("fresh", 3.0)
    assert tab.badge.text().startswith(DOT + " Fresh: last run")
    tab.set_freshness("stale", 50.0)
    assert tab.badge.text().startswith(DOT + " Stale: last run")
    assert DASH not in tab.badge.text()


def test_tracker_next_step_carries_no_em_dash(qtbot):
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    w = MainWindow(csv_paths=[], registry=reg)
    qtbot.addWidget(w)
    today = date.today()
    fresh = (today - timedelta(days=1)).isoformat()
    stale = (today - timedelta(days=w.followup_days + 5)).isoformat()
    rows = {
        "due": {"job_posting_id": "due", "status": "applied", "applied_date": stale},
        "sent": {"job_posting_id": "sent", "status": "applied", "applied_date": stale,
                 "followed_up_at": "2026-09-01"},
        "int": {"job_posting_id": "int", "status": "interviewing", "applied_date": fresh},
        "offer": {"job_posting_id": "offer", "status": "offer", "applied_date": fresh},
        "rej": {"job_posting_id": "rej", "status": "rejected", "applied_date": fresh},
        "wait": {"job_posting_id": "wait", "status": "applied", "applied_date": fresh},
    }
    w._tracked = rows
    steps = {jid: w._tracker_card_info(jid)["next_step"] for jid in rows}
    assert all(steps.values()), steps
    assert {jid: s for jid, s in steps.items() if DASH in s} == {}
    assert steps["due"].startswith("No reply in") and ": send a short follow-up" in steps["due"]
    assert steps["rej"] == "Rejected: no action needed."


def test_resume_data_stale_banner_carries_no_em_dash(qtbot, master_tmp):
    ed = ResumeDataEditor(master_path=master_tmp)
    qtbot.addWidget(ed)
    labels = [lbl.text() for lbl in ed.stale_banner.findChildren(QtWidgets.QLabel)]
    assert any(t.startswith("resume.md is older than your Resume Data,") for t in labels), labels
    assert all(DASH not in t for t in labels)
