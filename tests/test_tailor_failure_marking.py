"""Failed tailor runs must be VISIBLE and RECORDED, batch crash or not.

Three layers under test:
  seen_db   — a `tailor_failures` table (record / clear / ids), cleared by a
              later successful record_resume for the same job.
  jobs_model/theme/jobs_tab — a "tailor_failed" row tag that tints the row red
              on the unseen (high) tab and appears in its legend.
  main_window — per-job incremental recording: every job's outcome lands in
              the registry the moment it finishes (queued signal onto the UI
              thread), so an interrupted batch keeps everything already done;
              and the thread pool is bounded so a 14-job batch can't stampede
              the Gemini quota.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtCore

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import seen_db  # noqa: E402
from qt import jobs_tab, main_window as mw, theme  # noqa: E402
from qt.jobs_model import JobsTableModel  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402


# ---- seen_db.tailor_failures --------------------------------------------------


@pytest.fixture
def reg(tmp_path):
    r = seen_db.SeenRegistry(tmp_path / "seen.db")
    yield r
    r.close()


def test_record_and_list_tailor_failure(reg):
    reg.record_tailor_failure("J1", "Gemini call failed after retries")
    reg.record_tailor_failure("J2", "429")
    assert reg.tailor_failure_ids() == {"J1", "J2"}
    assert reg.tailor_failures()["J1"] == "Gemini call failed after retries"


def test_record_tailor_failure_upserts_latest_error(reg):
    reg.record_tailor_failure("J1", "first")
    reg.record_tailor_failure("J1", "second")
    assert reg.tailor_failures() == {"J1": "second"}


def test_clear_tailor_failure_is_idempotent(reg):
    reg.record_tailor_failure("J1", "err")
    reg.clear_tailor_failure("J1")
    reg.clear_tailor_failure("J1")  # absent -> no-op
    assert reg.tailor_failure_ids() == set()


def test_successful_resume_clears_the_failure(reg):
    reg.record_tailor_failure("J1", "err")
    reg.record_resume("J1", r"C:\out\folder")
    assert reg.tailor_failure_ids() == set()
    assert reg.resume_path("J1") == r"C:\out\folder"


def test_failures_survive_reopen(tmp_path):
    with seen_db.SeenRegistry(tmp_path / "seen.db") as r:
        r.record_tailor_failure("J9", "boom")
    with seen_db.SeenRegistry(tmp_path / "seen.db") as r:
        assert r.tailor_failure_ids() == {"J9"}


# ---- row tag + theme + legend --------------------------------------------------


def _model_df():
    return pd.DataFrame([
        {"job_posting_id": "J1", "recommendation": "apply"},
        {"job_posting_id": "J2", "recommendation": "consider"},
    ])


def test_failed_tag_wins_on_high_tab():
    m = JobsTableModel(["job_posting_id"], mode="high")
    m.set_dataframe(_model_df(), resume_ids={"J1"}, failed_ids={"J1", "J2"})
    # A fresh failure outranks a stale earlier resume — red means "re-run me".
    assert m.row_tag(0) == "tailor_failed"
    assert m.row_tag(1) == "tailor_failed"


def test_no_failed_ids_keeps_existing_tags():
    m = JobsTableModel(["job_posting_id"], mode="high")
    m.set_dataframe(_model_df(), resume_ids={"J1"})
    assert m.row_tag(0) == "has_resume"
    assert m.row_tag(1) == "consider"


def test_all_tab_stays_untinted():
    m = JobsTableModel(["job_posting_id"], mode="all")
    m.set_dataframe(_model_df(), failed_ids={"J1"})
    assert m.row_tag(0) == ""


def test_theme_and_legend_carry_the_failed_tint():
    assert theme.row_color("tailor_failed").isValid()
    legend = jobs_tab.legend_items_for("high")
    assert any("fail" in label.lower() for _color, label in legend)


# ---- MainWindow: incremental per-job recording + bounded pool -------------------


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.all_ids.return_value = set()
    reg.tailor_failure_ids.return_value = set()
    return reg


def _win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def test_on_tailor_job_done_records_success(qtbot):
    w = _win(qtbot)
    w._on_tailor_job_done({"id": "J1", "label": "X", "dir": r"C:\out\J1", "error": None})
    w.registry.record_resume.assert_called_once_with("J1", r"C:\out\J1")
    w.registry.record_tailor_failure.assert_not_called()


def test_on_tailor_job_done_records_failure(qtbot):
    w = _win(qtbot)
    w._on_tailor_job_done({"id": "J1", "label": "X", "dir": None, "error": "429 quota"})
    w.registry.record_tailor_failure.assert_called_once_with("J1", "429 quota")
    w.registry.record_resume.assert_not_called()


def test_on_tailor_job_done_survives_registry_errors(qtbot):
    w = _win(qtbot)
    w.registry.record_resume.side_effect = RuntimeError("db locked")
    w._on_tailor_job_done({"id": "J1", "label": "X", "dir": "d", "error": None})  # no raise


def test_tailor_work_emits_one_result_per_job_incrementally(qtbot, monkeypatch):
    import resume_tailor

    w = _win(qtbot)
    monkeypatch.setattr(resume_tailor, "tailor",
                        lambda job, **kw: f"out/{job['job_posting_id']}")
    seen = []

    def rec(result):  # a real function: PySide6 weak-refs transient bound methods
        seen.append(result)

    w.tailor_job_done.connect(rec)  # queued to the UI thread (emit is off-thread)
    jobs = [{"job_posting_id": f"J{i}", "job_title": "T", "company_name": "C"}
            for i in range(5)]
    opts = {"cover_letter": False, "ats_report": False, "prep_sheet": False,
            "tone": "professional"}
    results = w._tailor_work(jobs, opts)
    assert len(results) == 5
    qtbot.waitUntil(lambda: len(seen) == 5, timeout=5000)  # pump queued deliveries
    assert {r["id"] for r in seen} == {f"J{i}" for i in range(5)}
    assert all(r["dir"] == f"out/{r['id']}" for r in seen)


def test_tailor_pool_is_bounded():
    assert mw._tailor_pool_size(1) == 1
    assert mw._tailor_pool_size(3) == 3
    assert mw._tailor_pool_size(14) == mw.MAX_PARALLEL_TAILORS
    assert mw._tailor_pool_size(0) == 1


def test_apply_df_views_passes_failed_ids_to_tabs(qtbot):
    w = _win(qtbot)
    w.registry.tailor_failure_ids.return_value = {"1"}
    w.df = pd.DataFrame([
        {"job_posting_id": "1", "job_title": "Eng", "company_name": "Acme",
         "score": "5", "is_seen": "no", "url": "http://x/1",
         "recommendation": "apply"},
    ])
    w._apply_df_views()
    assert w.high_tab.model.row_tag(0) == "tailor_failed"
