"""SP10: the Qt manual-add form + its wiring through MainWindow (headless, LLM mocked).

Runs under QT_QPA_PLATFORM=offscreen (set in conftest). The dialog is a thin shell;
the pipeline is exercised via the mocked manual_add module so no real Gemini/network
call happens.
"""
from unittest.mock import MagicMock

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
    dlg._on_accept(do_tailor=True)
    assert dlg.result() == QtWidgets.QDialog.DialogCode.Accepted
    vals = dlg.values()
    assert vals["jd_text"].startswith("Data Analyst")
    assert vals == {"jd_text": vals["jd_text"], "url": "https://x/1",
                    "title": "ML Engineer", "company": "Globex", "do_tailor": True}


def test_dialog_just_score_requires_title_company_jd_not_url(qtbot, monkeypatch):
    dlg = ManualAddDialog()
    qtbot.addWidget(dlg)
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("w", True)))
    dlg._on_accept(do_tailor=False)            # nothing entered -> blocked
    assert warned.get("w")
    assert dlg.result() != QtWidgets.QDialog.DialogCode.Accepted
    warned.clear()
    dlg.title.setText("DA")
    dlg.company.setText("Acme")
    dlg.jd.setPlainText(_JD)                    # title+company+JD (no URL) is enough to score
    dlg._on_accept(do_tailor=False)
    assert not warned.get("w")
    assert dlg.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert dlg.values()["do_tailor"] is False


def test_dialog_score_and_tailor_requires_url_too(qtbot, monkeypatch):
    dlg = ManualAddDialog()
    qtbot.addWidget(dlg)
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("w", True)))
    dlg.title.setText("DA")
    dlg.company.setText("Acme")
    dlg.jd.setPlainText(_JD)
    dlg._on_accept(do_tailor=True)             # URL missing -> blocked for tailoring
    assert warned.get("w")
    assert dlg.result() != QtWidgets.QDialog.DialogCode.Accepted
    warned.clear()
    dlg.url.setText("https://x/1")
    dlg._on_accept(do_tailor=True)
    assert not warned.get("w")
    assert dlg.values()["do_tailor"] is True


def test_dialog_edit_mode_prefills_and_requires_url(qtbot, monkeypatch):
    initial = {"url": "https://x/9", "title": "Old T", "company": "Old C", "jd_text": _JD}
    dlg = ManualAddDialog(edit_mode=True, initial=initial)
    qtbot.addWidget(dlg)
    assert dlg.url.text() == "https://x/9"
    assert dlg.title.text() == "Old T"
    assert dlg.company.text() == "Old C"
    assert dlg.jd.toPlainText().startswith("Data Analyst")
    warned = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("w", True)))
    dlg.url.setText("")                        # edit requires the URL too
    dlg._on_accept(do_tailor=False)
    assert warned.get("w")
    assert dlg.result() != QtWidgets.QDialog.DialogCode.Accepted


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


# ── cover-letter prompt is conditional on "Score + tailor" ────────────────────

def test_just_score_skips_cover_letter_prompt(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(ManualAddDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ManualAddDialog, "values",
                        lambda self: {"jd_text": _JD, "url": "", "title": "T",
                                      "company": "C", "do_tailor": False})
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    asked = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.setdefault("q", True)))
    launched = {}
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: launched.update(fn=fn))
    add_calls = {}
    monkeypatch.setattr(manual_add, "add_manual_job",
                        lambda **k: add_calls.update(k) or {"record": {}, "resume_dir": None,
                                                            "appended": True})
    w._add_manual_job_dialog()
    assert "q" not in asked                 # NOT asked about a cover letter for "just score"
    launched["fn"]()
    assert add_calls["do_tailor"] is False
    assert add_calls["tailor_opts"]["cover_letter"] is False


def test_score_and_tailor_shows_cover_letter_prompt(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(ManualAddDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ManualAddDialog, "values",
                        lambda self: {"jd_text": _JD, "url": "https://x/1", "title": "T",
                                      "company": "C", "do_tailor": True})
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    asked = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.setdefault("q", True) or
                                     QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: None)
    w._add_manual_job_dialog()
    assert asked.get("q") is True           # cover-letter prompt IS shown for tailoring
    assert w._manual_adding is True


# ── delete + edit job handlers ────────────────────────────────────────────────

def test_delete_jobs_confirms_clears_and_reloads(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    deleted = {}
    monkeypatch.setattr(mw.jobsdata, "delete_jobs",
                        lambda ids, **k: deleted.update(ids=list(ids)) or len(list(ids)))
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))
    w._delete_jobs(["manual-1", "123"])
    assert deleted["ids"] == ["manual-1", "123"]
    w.registry.clear_status.assert_any_call("manual-1")
    assert reloaded == [True]


def test_delete_jobs_cancel_is_noop(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    called = []
    monkeypatch.setattr(mw.jobsdata, "delete_jobs", lambda ids, **k: called.append(ids))
    w._delete_jobs(["manual-1"])
    assert called == []


def test_edit_manual_job_prefills_and_updates_keeping_id(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "master_row",
                        lambda jid, **k: {"job_posting_id": jid, "url": "https://old",
                                          "job_title": "Old", "company_name": "OldCo",
                                          "job_description_formatted": _JD, "score": 5})
    monkeypatch.setattr(ManualAddDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ManualAddDialog, "values",
                        lambda self: {"jd_text": _JD, "url": "https://new", "title": "New",
                                      "company": "NewCo", "do_tailor": False})
    updated = {}
    monkeypatch.setattr(mw.jobsdata, "update_manual_job",
                        lambda record, **k: updated.update(record=record, kw=k) or True)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    w._edit_manual_job("manual-abc")
    rec = updated["record"]
    assert rec["job_posting_id"] == "manual-abc"     # identity preserved across edit
    assert rec["url"] == "https://new" and rec["job_title"] == "New"
    assert rec["company_name"] == "NewCo"
    assert rec["score"] == 5                          # score carried over (field-fix only)
    assert updated["kw"].get("old_id") == "manual-abc"
