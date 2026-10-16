"""The dashboard reads one job's description from the cache when it needs it.

The loaded frame no longer carries the four description columns (see
load_files' `descriptions_cached` mode), so the two readers of that text, the
detail card and the tailor / cover letter / Ask AI payload, fill a row from
`jobsdata.job_description(job_id, row["_source"])`. A frame that still carries
the columns (older callers, synthetic test rows) is used exactly as before.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import desc_cache
import jobsdata
from qt.main_window import LoadedFrames, MainWindow

_MD = "## Data Analyst\n\n- Build dashboards in SQL and Python\n- Own the dbt models\n"
_SUMMARY = "Acme is hiring a data analyst to build dashboards and own the models. " * 3


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.all_ids.return_value = set()
    return reg


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "MIRROR_DIR", tmp_path / "mirror")
    monkeypatch.setattr(jobsdata, "load_removed_jobs", lambda: set())
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
    return path


@pytest.fixture
def sources(tmp_path):
    master = _write(tmp_path / "drive" / "linkedin_jobs_master.csv.gz", [
        {"job_posting_id": "1", "job_title": "Data Analyst", "company_name": "Acme",
         "url": "https://x/1", "is_seen": "no", "job_summary": "short master stub"},
        {"job_posting_id": "2", "job_title": "Engineer", "company_name": "Beta",
         "url": "https://x/2", "is_seen": "no", "job_summary": _SUMMARY},
    ])
    run = _write(tmp_path / "repo" / "daily" / "2026-10-16_scored.csv.gz", [
        {"job_posting_id": "1", "job_title": "Data Analyst", "company_name": "Acme",
         "url": "https://x/1", "is_seen": "no", "job_summary": _SUMMARY,
         "job_description_md": _MD, "reason": "Strong SQL match"},
    ])
    return [master, run]


def _install(win, paths):
    df, id_to_path = jobsdata.load_files(paths, descriptions_cached=True)
    assert not set(desc_cache.DESC_COLS) & set(df.columns)
    win._apply_frames(LoadedFrames(df, id_to_path, None, 36, (), ()))


def test_load_frames_asks_for_the_cached_mode(win, sources):
    win.csv_paths = sources
    loaded = win._load_frames()
    assert not set(desc_cache.DESC_COLS) & set(loaded.df.columns)
    assert sorted(loaded.df["job_posting_id"]) == ["1", "2"]


def test_the_card_shows_the_description_from_the_cache(win, sources):
    _install(win, sources)
    win._show_preview("1")
    assert not win.preview.desc_toggle.isHidden()
    text = win.preview.desc_view.toPlainText()
    assert "Build dashboards in SQL and Python" in text
    assert "##" not in text                           # md rendered, as before


def test_the_card_shows_a_master_only_jobs_summary(win, sources):
    _install(win, sources)
    win._show_preview("2")
    assert win.preview.desc_view.toPlainText().startswith("Acme is hiring")


def test_the_payload_carries_the_description_from_the_cache(win, sources):
    _install(win, sources)
    payload = win._job_payload("1")
    assert payload["job_description_md"] == _MD
    assert payload["job_summary"] == _SUMMARY
    assert payload["job_description_formatted"] == ""
    assert payload["company_name"] == "Acme"
    assert win._job_payload("2")["job_summary"] == _SUMMARY


def test_the_hydrated_payload_needs_no_master_scan(win, sources, monkeypatch):
    from qt import mw_tailor
    _install(win, sources)
    monkeypatch.setattr(mw_tailor.jobsdata, "master_row",
                        lambda *a, **k: pytest.fail("master scanned"))
    assert mw_tailor._with_master_jd(win._job_payload("1"))["job_description_md"] == _MD


def test_a_job_with_nothing_cached_still_renders(win, sources, monkeypatch):
    _install(win, sources)
    monkeypatch.setattr(jobsdata, "job_description", lambda jid, source: {})
    win._show_preview("1")
    assert win.preview.desc_toggle.isHidden()
    assert win._job_payload("1")["job_description_md"] == ""


def test_a_frame_that_carries_the_columns_is_used_as_is(win, monkeypatch):
    monkeypatch.setattr(jobsdata, "job_description",
                        lambda *a: pytest.fail("cache read for a full frame"))
    win.df = pd.DataFrame([{"job_posting_id": "9", "job_title": "Analyst",
                            "company_name": "Acme", "_source": "x.csv.gz",
                            "job_summary": _SUMMARY, "job_description_md": "",
                            "job_description_formatted": "", "job_description": ""}])
    win._row_by_id = {"9": 0}
    assert win._job_payload("9")["job_summary"] == _SUMMARY
    win._show_preview("9")
    assert win.preview.desc_view.toPlainText().startswith("Acme is hiring")


def test_a_row_that_did_not_come_from_a_file_reads_no_cache(win, monkeypatch):
    monkeypatch.setattr(jobsdata, "job_description",
                        lambda *a: pytest.fail("cache read for a synthetic row"))
    win.df = pd.DataFrame([{"job_posting_id": "9", "job_title": "Analyst",
                            "company_name": "Acme"}])
    win._row_by_id = {"9": 0}
    assert win._job_payload("9")["job_summary"] == ""
    win._show_preview("9")
    assert win.preview.desc_toggle.isHidden()


def test_a_manual_jobs_cached_summary_still_sends_the_tailor_to_the_master(
        win, tmp_path, monkeypatch):
    # A hand-added job's source row holds only the 1000-character job_summary.
    # Filling the payload from the cache must not make it look described: the
    # tailor still reads the full text from the master.
    from qt import mw_tailor
    mid = "manual-20261016-acme"
    assert jobsdata.is_manual_job_id(mid)
    manual = _write(tmp_path / "drive" / "manual" / "manual_jobs_scored.csv.gz", [
        {"job_posting_id": mid, "job_title": "Data Analyst", "company_name": "Acme",
         "url": "https://x/m", "is_seen": "no", "job_summary": "S" * 1000},
    ])
    _install(win, [manual])
    payload = win._job_payload(mid)
    assert payload["job_summary"] == "S" * 1000
    asked = []

    def master_row(jid, **k):
        asked.append(jid)
        return {"job_description_formatted": "<p>the whole posting</p>",
                "job_description": "the whole posting"}
    monkeypatch.setattr(mw_tailor.jobsdata, "master_row", master_row)
    job = mw_tailor._with_master_jd(payload)
    assert asked == [mid]
    assert job["job_description_formatted"] == "<p>the whole posting</p>"
    assert job["job_summary"] == "S" * 1000
