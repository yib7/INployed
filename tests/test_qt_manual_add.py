"""SP10: the Qt manual-add form + its wiring through MainWindow (headless, LLM mocked).

Runs under QT_QPA_PLATFORM=offscreen (set in conftest). The dialog is a thin shell;
the pipeline is exercised via the mocked manual_add module so no real Gemini/network
call happens.
"""
from unittest.mock import MagicMock

import pytest
from PySide6 import QtWidgets

import manual_add
from qt import main_window as mw
from qt.main_window import MainWindow
from qt.manual_add_dialog import ManualAddDialog

_JD = (
    "Data Analyst\nAcme Corp\n"
    "Build dashboards in SQL and Python; communicate findings. Entry-level welcome.\n"
) * 3


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    return reg


def _win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


# ── the dialog itself ─────────────────────────────────────────────────────────

def test_dialog_collects_values(qtbot):
    dlg = ManualAddDialog()
    qtbot.addWidget(dlg)
    dlg.jd.setPlainText(_JD)
    dlg.url.setText("https://x/1")
    dlg.title.setText("ML Engineer")
    dlg.company.setText("Globex")
    vals = dlg.values()
    assert vals["jd_text"].startswith("Data Analyst")
    assert vals == {"jd_text": vals["jd_text"], "url": "https://x/1",
                    "title": "ML Engineer", "company": "Globex"}


def test_dialog_blocks_accept_with_no_input(qtbot, monkeypatch):
    dlg = ManualAddDialog()
    qtbot.addWidget(dlg)
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("w", True)))
    dlg._on_accept()                       # nothing entered
    assert warned.get("w")                 # a warning was shown...
    assert dlg.result() != QtWidgets.QDialog.DialogCode.Accepted  # ...and accept was blocked


def test_dialog_accepts_with_only_a_url(qtbot):
    dlg = ManualAddDialog()
    qtbot.addWidget(dlg)
    dlg.url.setText("https://x/1")
    dlg._on_accept()                       # a URL alone is enough to submit
    assert dlg.result() == QtWidgets.QDialog.DialogCode.Accepted


# ── the button exists on the discovery tabs ───────────────────────────────────

def _tab_button_texts(tab):
    bar = tab._bar
    return [bar.itemAt(i).widget().text()
            for i in range(bar.count())
            if isinstance(bar.itemAt(i).widget(), QtWidgets.QPushButton)]


def test_add_job_button_on_high_and_all_tabs(qtbot):
    w = _win(qtbot)
    assert "Add job by hand" in _tab_button_texts(w.high_tab)
    assert "Add job by hand" in _tab_button_texts(w.all_tab)


# ── the worker body runs the pipeline (manual_add mocked) ─────────────────────

def test_manual_add_work_calls_pipeline(qtbot, monkeypatch):
    w = _win(qtbot)
    captured = {}

    def fake_add(**kwargs):
        captured.update(kwargs)
        return {"record": {"job_posting_id": "manual-1", "source": "manual"},
                "resume_dir": "/tmp/x", "appended": True}

    monkeypatch.setattr(manual_add, "add_manual_job", fake_add)
    vals = {"jd_text": _JD, "url": "https://x/1", "title": "T", "company": "C"}
    opts = {"cover_letter": False, "ats_report": True, "prep_sheet": False,
            "tone": "professional"}
    out = w._manual_add_work(vals, opts)
    assert out["appended"] is True
    assert captured["jd_text"] == _JD and captured["url"] == "https://x/1"
    assert captured["tailor_opts"] == opts
    assert callable(captured["on_status"])    # progress is streamed to the status bar


def test_add_manual_job_dialog_launches_worker(qtbot, monkeypatch):
    """The form -> cover-letter prompt -> off-thread worker wiring (no real work)."""
    w = _win(qtbot)
    # accept the dialog with values, decline the cover letter, capture the worker fn
    monkeypatch.setattr(ManualAddDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ManualAddDialog, "values",
                        lambda self: {"jd_text": _JD, "url": "", "title": "", "company": ""})
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    launched = {}
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: launched.update(
                            fn=fn, on_done=on_done, on_error=on_error))
    add_calls = {}
    monkeypatch.setattr(manual_add, "add_manual_job",
                        lambda **k: add_calls.update(k) or {"record": {}, "resume_dir": None,
                                                            "appended": True})
    w._add_manual_job_dialog()
    assert "fn" in launched and w._manual_adding is True
    # running the captured worker fn calls the pipeline with the cover-letter choice
    launched["fn"]()
    assert add_calls["jd_text"] == _JD
    assert add_calls["tailor_opts"]["cover_letter"] is False


def test_add_manual_job_dialog_cancel_is_noop(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(ManualAddDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Rejected)
    ran = []
    monkeypatch.setattr(mw.workers, "run_async", lambda *a, **k: ran.append(True))
    w._add_manual_job_dialog()
    assert ran == [] and not getattr(w, "_manual_adding", False)


# ── finish handlers: record the resume, merge sources, report ─────────────────

def test_finish_manual_add_records_resume_and_reloads(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "local_run_files", lambda *a, **k: [])
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))
    w._manual_adding = True
    w._finish_manual_add({
        "record": {"job_posting_id": "manual-1", "job_title": "DA",
                   "company_name": "Acme", "score": 5, "source": "manual"},
        "resume_dir": "/tmp/Generated/Acme", "appended": True})
    w.registry.record_resume.assert_called_once_with("manual-1", "/tmp/Generated/Acme")
    assert reloaded == [True] and w._manual_adding is False
    assert "Acme" in w.statusBar().currentMessage()


def test_finish_manual_add_merges_manual_source(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    new_gz = tmp_path / "manual" / "manual_jobs_scored.csv.gz"
    monkeypatch.setattr(mw.jobsdata, "local_run_files", lambda *a, **k: [new_gz])
    monkeypatch.setattr(w, "reload_data", lambda: None)
    w._finish_manual_add({"record": {"job_posting_id": "manual-1"},
                          "resume_dir": None, "appended": True})
    assert new_gz in w.csv_paths               # the manual scored file is now a source


def test_finish_manual_add_error_shows_dialog(qtbot, monkeypatch):
    w = _win(qtbot)
    shown = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.setdefault("msg", a)))
    w._manual_adding = True
    w._finish_manual_add_error(RuntimeError("no usable JD"))
    assert shown.get("msg") and w._manual_adding is False
    assert "no usable JD" in w.statusBar().currentMessage()
