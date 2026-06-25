"""SP4: score preview visibility + the worker-backed job actions (mocked backends)."""
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtWidgets

from qt import main_window as mw
from qt.main_window import PREVIEW_TABS, TAB_TITLES, MainWindow


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    return reg


def _win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def test_scraper_and_scorer_cmd(qtbot):
    w = _win(qtbot)
    assert w.scraper_cmd(False)[-1].endswith("scraper.py")
    assert "--max-keywords" in w.scraper_cmd(True)
    assert w.scorer_cmd()[-1].endswith("score_jobs.py")


def test_console_python_swaps_pythonw_for_python(monkeypatch):
    # pythonw has no usable stdout -> children must run on the console python
    monkeypatch.setattr(mw.os.path, "exists", lambda p: True)
    assert mw._console_python(r"C:\Py\pythonw.exe").lower().endswith("python.exe")
    # but only when the sibling python.exe actually exists
    monkeypatch.setattr(mw.os.path, "exists", lambda p: False)
    assert mw._console_python(r"C:\Py\pythonw.exe").lower().endswith("pythonw.exe")
    # a normal interpreter passes straight through
    assert mw._console_python(r"C:\Py\python.exe").lower().endswith("python.exe")


def test_scale_bar_nudges_clamp_and_persist(qtbot, monkeypatch):
    # Cycle 17 SP1: the bottom scale bar drives one persisted scale, 10% steps,
    # clamped to [50, 200], persisted via jobsdata (not the settings schema).
    w = _win(qtbot)
    saved = {}
    monkeypatch.setattr(mw.jobsdata, "save_ui_scale_pct", lambda p: saved.__setitem__("pct", p))
    scaled = {}
    monkeypatch.setattr(mw.theme, "set_scale", lambda app, s: scaled.__setitem__("s", s))

    w._apply_scale(120)
    assert w._ui_scale_pct == 120
    assert scaled["s"] == pytest.approx(1.2)
    assert saved.get("pct") == 120                 # persisted as an int via jobsdata

    w._apply_scale(9999)        # clamp high
    assert w._ui_scale_pct == 200
    w._apply_scale(1)           # clamp low (cycle 17 floor = 50)
    assert w._ui_scale_pct == 50

    w._apply_scale(100)
    w._nudge_scale(10)
    assert w._ui_scale_pct == 110               # + button steps 10
    w._nudge_scale(-60)
    assert w._ui_scale_pct == 50                # 50 floor

    # the slider spans 50-200 in 10% steps; the Ctrl-zoom shortcuts are gone
    assert w._scale_slider.minimum() == 50 and w._scale_slider.maximum() == 200
    assert not hasattr(w, "_zoom")


def _tracker_button_texts(tab):
    bar = tab._bar
    return [bar.itemAt(i).widget().text()
            for i in range(bar.count())
            if isinstance(bar.itemAt(i).widget(), QtWidgets.QPushButton)]


def test_tracker_toolbar_has_no_set_status_button(qtbot):
    # Cycle 16 SP3: Set status removed from the toolbar (right-click status covers it).
    w = _win(qtbot)
    texts = _tracker_button_texts(w.tracker_tab)
    assert "Set status" not in texts
    assert "Mark followed up" in texts             # the other tracker actions remain
    assert not hasattr(w, "_tracker_set_status")   # the dead handler is gone
    assert hasattr(w.tracker_tab, "apply_status")  # right-click status path intact


class _FakeProc:
    def __init__(self, lines, rc):
        self.stdout = iter(lines)
        self._rc = rc

    def wait(self):
        return self._rc


def test_scrape_work_success_returns_true(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    monkeypatch.setattr(mw, "APPDATA", tmp_path)
    monkeypatch.setattr(mw.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(["ok\n"], 0))
    assert w._scrape_work(True) is True
    assert "ok" in (tmp_path / "scrape.log").read_text(encoding="utf-8")


def test_scrape_work_raises_with_captured_output_on_failure(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    monkeypatch.setattr(mw, "APPDATA", tmp_path)
    monkeypatch.setattr(mw.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(["scraping...\n", "BOOM bad token\n"], 2))
    with pytest.raises(RuntimeError) as ei:
        w._scrape_work(True)
    msg = str(ei.value)
    assert "BOOM bad token" in msg and "exit 2" in msg  # real error surfaced, not "check the console"
    assert "BOOM bad token" in (tmp_path / "scrape.log").read_text(encoding="utf-8")


def test_after_scrape_merges_local_run_files(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    new_file = tmp_path / "evening" / "x_scored.csv.gz"
    monkeypatch.setattr(mw.jobsdata, "local_run_files", lambda *a, **k: [new_file])
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))
    w._scraping = True
    w._after_scrape(True)
    assert new_file in w.csv_paths  # local scrape output now a dashboard source
    assert reloaded and w._scraping is False
    # idempotent: a second call must not duplicate the path
    w._after_scrape(True)
    assert w.csv_paths.count(new_file) == 1


def test_after_scrape_error_shows_dialog(qtbot, monkeypatch):
    w = _win(qtbot)
    shown = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.setdefault("msg", a)))
    w._scraping = True
    w._after_scrape_error(RuntimeError("scraper.py failed (exit 1).\n\ndetails"))
    assert shown.get("msg") and w._scraping is False


def test_preview_visible_only_on_job_tabs(qtbot):
    w = _win(qtbot)
    for i, title in enumerate(TAB_TITLES):
        w.tabs.setCurrentIndex(i)
        assert w._preview_shown == (title in PREVIEW_TABS)


def test_show_preview_renders_segments(qtbot):
    w = _win(qtbot)
    w.df = pd.DataFrame([{"job_posting_id": "1", "job_title": "Data Analyst",
                          "company_name": "Acme", "score": "5", "reason": "great fit",
                          "strengths": "python|sql", "gaps": "go"}])
    w._row_by_id = {"1": 0}
    w._show_preview("1")
    text = w.preview.toPlainText()
    assert "Acme" in text and "great fit" in text


def test_mark_ids_seen_writes_registry_and_reloads(qtbot, monkeypatch):
    w = _win(qtbot)
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(True))
    w.id_to_path = {}
    w._mark_ids_seen(["1", "2"])
    w.registry.mark.assert_called_once_with(["1", "2"])
    assert reloaded


def test_set_status_for_calls_registry(qtbot):
    w = _win(qtbot)
    w._set_status_for(["1"], "applied")
    assert w.registry.set_status.called


def test_set_status_applied_also_marks_seen(qtbot, monkeypatch):
    # 'applied' via the right-click menu must also mark the job seen (what the old
    # 'Mark applied' button did) — non-'applied' statuses must not.
    w = _win(qtbot)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    w.id_to_path = {}
    w._set_status_for(["1"], "applied")
    assert w.registry.set_status.called and w.registry.mark.called
    w.registry.mark.reset_mock()
    w._set_status_for(["2"], "rejected")
    assert not w.registry.mark.called


def test_seen_undo(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    w.id_to_path = {}
    assert not w.btn_undo_seen.isEnabled()
    w._mark_ids_seen(["1", "2"])
    assert w._seen_undo == [["1", "2"]]
    assert w.btn_undo_seen.isEnabled()
    w._undo_seen()
    w.registry.unmark.assert_called_once_with(["1", "2"])  # the seen rows are removed
    assert w._seen_undo == []
    assert not w.btn_undo_seen.isEnabled()


def test_on_fs_change_schedules_debounced_reload(qtbot, monkeypatch):
    w = _win(qtbot)
    called = []
    monkeypatch.setattr(w, "reload_data", lambda: called.append(True))
    w._on_fs_change("whatever")
    assert w._reload_timer.isActive()   # a burst of events coalesces into one reload
    w._auto_reload()
    assert called


def test_fs_watcher_watches_source_files(qtbot, tmp_path):
    f = tmp_path / "run_scored.csv.gz"
    f.write_bytes(b"not really gzip")  # load skips it; the watcher still arms on it
    w = MainWindow(csv_paths=[f], registry=_fake_registry())
    qtbot.addWidget(w)
    assert str(f) in set(w._fs_watcher.files())


def test_poll_reloads_only_when_sources_change(qtbot, monkeypatch):
    # The poll is the fallback for setups that emit no file events: it reloads
    # only when the on-disk signature drifts, so there's no need for a Refresh button.
    w = _win(qtbot)
    called = []
    monkeypatch.setattr(w, "reload_data", lambda: called.append(True))
    w._poll_for_changes()
    assert not called                 # signature unchanged -> no reload
    w._source_sig = ("stale",)        # a source changed on disk
    w._poll_for_changes()
    assert called


def test_apply_work_opens_url(qtbot, monkeypatch):
    w = _win(qtbot)
    import resume_tailor.apply as apply_mod
    monkeypatch.setattr(apply_mod, "resolve_generated_dir", lambda **k: "folder")
    monkeypatch.setattr(apply_mod, "build_apply_context",
                        lambda folder: {"apply_url": "https://x/1", "job": {"company": "Acme"}})
    opened = []
    monkeypatch.setattr(mw.chrome, "open_in_chrome", opened.append)
    ctx = w._apply_work("1", {"job_posting_id": "1"})
    assert ctx["apply_url"] == "https://x/1"
    assert opened == ["https://x/1"]


def test_apply_button_is_rightmost(qtbot):
    w = _win(qtbot)
    btns = [w._action_bar.itemAt(i).widget() for i in range(w._action_bar.count())]
    btns = [b for b in btns if isinstance(b, QtWidgets.QPushButton)]
    assert btns[-1].text() == "Apply"


def test_apply_button_disabled_without_resume(qtbot):
    w = _win(qtbot)
    w.registry.resume_path.return_value = None
    w._update_apply_button("123")
    assert w.btn_apply.isEnabled() is False


def test_apply_button_enabled_when_pdf_and_md_on_disk(qtbot, tmp_path, monkeypatch):
    from resume_tailor import output
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Cand")
    (tmp_path / output.resume_filename()).write_bytes(b"%PDF")
    (tmp_path / "apply.md").write_text("# sheet", encoding="utf-8")
    w = _win(qtbot)
    w.registry.resume_path.return_value = str(tmp_path)
    w._update_apply_button("123")
    assert w.btn_apply.isEnabled() is True
    assert w.btn_apply.property("applyReady") is True


def test_apply_button_not_ready_when_md_missing(qtbot, tmp_path, monkeypatch):
    from resume_tailor import output
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Cand")
    (tmp_path / output.resume_filename()).write_bytes(b"%PDF")  # PDF but no apply.md
    w = _win(qtbot)
    w.registry.resume_path.return_value = str(tmp_path)
    w._update_apply_button("123")
    assert w.btn_apply.isEnabled() is False


_APPLY_CTX = {
    "job": {"company": "Acme", "title": "Engineer", "job_posting_id": "1"},
    "resume_pdf": "C:/Generated/Acme/Engineer/Cand_Resume.pdf",
    "cover_letter_pdf": "",
    "apply_md": "# Apply sheet — Engineer @ Acme\n\nPaste me into Claude-in-Chrome.",
    "apply_md_path": "C:/Generated/Acme/Engineer/apply.md",
    "apply_url": "http://x/1",
    "generated_dir": "C:/Generated/Acme/Engineer",
}


def test_finish_apply_opens_panel_and_hides_preview(qtbot):
    w = _win(qtbot)
    w.tabs.setCurrentIndex(0)  # a job tab → preview would normally show
    w._applying = True
    w._finish_apply_ok(dict(_APPLY_CTX))
    assert w._apply_panel_open is True
    assert w._preview_shown is False
    assert "Apply sheet" in w.apply_panel.current_sheet()


def test_close_apply_panel_restores_preview(qtbot):
    w = _win(qtbot)
    w.tabs.setCurrentIndex(0)
    w._finish_apply_ok(dict(_APPLY_CTX))
    w._close_apply_panel()
    assert w._apply_panel_open is False
    assert w._preview_shown is True  # restored on a job tab


def test_copy_apply_sheet_sets_clipboard(qtbot):
    w = _win(qtbot)
    w._finish_apply_ok(dict(_APPLY_CTX))
    w.apply_panel.copy_sheet()
    assert QtWidgets.QApplication.clipboard().text() == _APPLY_CTX["apply_md"]


def test_resume_ids_drops_folders_deleted_from_disk(qtbot, tmp_path):
    # The blue "tailored" tint follows on-disk existence: a recorded folder that
    # was deleted by hand no longer counts, while one still present does.
    w = _win(qtbot)
    live = tmp_path / "kept"
    live.mkdir()
    w.registry.resume_paths.return_value = {"1": str(live),
                                            "2": str(tmp_path / "gone")}
    assert w._resume_ids() == frozenset({"1"})   # "2" is gone -> tint cleared


def test_apply_sheet_preview_renders_markdown_keeps_raw_copy(qtbot):
    from qt.apply_panel import ApplyPanel
    p = ApplyPanel()
    qtbot.addWidget(p)
    raw = "# Heading One\n\nSome **bold** body text.\n\n<!-- inployed-apply-meta: {} -->"
    p.show_application({"apply_md": raw})
    # the viewer RENDERS markdown — its plain text drops the #/** syntax characters...
    rendered = p._sheet.toPlainText()
    assert "Heading One" in rendered and "bold body text" in rendered
    assert "#" not in rendered and "**" not in rendered
    # ...but the clipboard / current_sheet keep the RAW markdown source verbatim.
    assert p.current_sheet() == raw


def test_apply_sheet_pop_out_shows_sheet_and_copies(qtbot):
    # Cycle 16 SP6: an Expand button opens the apply sheet in a large window.
    from qt.apply_panel import ApplyPanel
    p = ApplyPanel()
    qtbot.addWidget(p)
    assert "Expand" in p._expand_btn.text()
    raw = "# Big Heading\n\nReadable **body** text."
    p.show_application({"apply_md": raw})
    p._pop_out()
    assert p._popout is not None and p._popout.isVisible()
    viewer = p._popout.findChild(QtWidgets.QTextBrowser)
    rendered = viewer.toPlainText()
    assert "Big Heading" in rendered and "body text" in rendered  # rendered markdown
    QtWidgets.QApplication.clipboard().setText("")
    p.copy_sheet()
    assert QtWidgets.QApplication.clipboard().text() == raw        # copy keeps raw md


def test_apply_panel_applied_button_invokes_callback(qtbot):
    from qt.apply_panel import ApplyPanel
    called = []
    p = ApplyPanel(on_applied=lambda: called.append(True))
    qtbot.addWidget(p)
    assert "applied" in p.applied_btn.text().lower()
    p.applied_btn.click()
    assert called == [True]


def test_i_applied_confirm_yes_records_in_tracker_and_closes(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    w.id_to_path = {}
    w.tabs.setCurrentIndex(0)
    w._finish_apply_ok(dict(_APPLY_CTX))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    w._mark_applied_from_panel()
    args, kwargs = w.registry.set_status.call_args
    assert args[0] == "1" and args[1] == "applied"      # uses the panel's stored job id
    assert kwargs.get("company") == "Acme"              # ...and its marker identity
    assert w.registry.mark.called                        # applied implies seen
    assert w._apply_panel_open is False                  # doubles as an exit button


def test_i_applied_confirm_no_leaves_everything_untouched(qtbot, monkeypatch):
    w = _win(qtbot)
    w._finish_apply_ok(dict(_APPLY_CTX))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    w._mark_applied_from_panel()
    assert not w.registry.set_status.called
    assert w._apply_panel_open is True                    # panel stays open on cancel


def test_tailor_work_runs_all_jobs_and_captures_failures(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    seen = []

    def fake_tailor(job, **k):
        seen.append(job["job_posting_id"])
        if job["job_posting_id"] == "2":
            raise RuntimeError("boom-2")
        return tmp_path / job["job_posting_id"]

    monkeypatch.setattr("resume_tailor.tailor", fake_tailor, raising=False)
    jobs = [{"job_posting_id": str(i), "company_name": "C", "job_title": "T"} for i in (1, 2, 3)]
    results = w._tailor_work(jobs, {"cover_letter": False, "ats_report": True,
                                    "prep_sheet": False, "tone": "professional"})
    assert sorted(seen) == ["1", "2", "3"]            # every job attempted (in parallel)
    by_id = {r["id"]: r for r in results}
    assert by_id["1"]["dir"] and by_id["1"]["error"] is None
    assert by_id["2"]["dir"] is None and "boom-2" in by_id["2"]["error"]   # failure captured
    assert by_id["3"]["dir"]


def test_tailor_work_streams_progress(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    captured = {}

    def fake_tailor(job, **k):
        captured["on_status"] = k.get("on_status")
        return tmp_path / job["job_posting_id"]

    monkeypatch.setattr("resume_tailor.tailor", fake_tailor, raising=False)
    jobs = [{"job_posting_id": "1", "company_name": "Acme", "job_title": "Eng"}]
    w._tailor_work(jobs, {"cover_letter": False, "ats_report": True,
                          "prep_sheet": False, "tone": "professional"})
    # the engine is handed a live progress callback, not the no-op default
    assert callable(captured["on_status"])
    # invoking it (same thread -> direct slot) updates the status bar with a real
    # progress line, never the old misleading "console" wording
    captured["on_status"]("selecting evidence")
    msg = w.statusBar().currentMessage()
    assert "console" not in msg
    assert "Tailoring" in msg and "done" in msg and "selecting evidence" in msg


def test_tailor_selected_status_drops_console_wording(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    monkeypatch.setattr(mw.workers, "run_async", lambda *a, **k: None)
    monkeypatch.setattr(w, "_job_payload",
                        lambda i: {"job_posting_id": i, "company_name": "C", "job_title": "T"})
    monkeypatch.setattr(w, "_selected_ids", lambda: ["1"])
    w._tailor_selected()
    assert "console" not in w.statusBar().currentMessage()


def test_finish_tailor_records_successes_and_reports(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    monkeypatch.setattr(mw.os, "startfile", lambda *_: None, raising=False)
    reloaded = []
    monkeypatch.setattr(w, "reload_data", lambda: reloaded.append(1))
    shown = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.setdefault("text", a[2])))
    w._tailoring = True
    results = [
        {"id": "1", "label": "Eng @ A", "dir": tmp_path / "1", "error": None},
        {"id": "2", "label": "Eng @ B", "dir": None, "error": "boom"},
    ]
    w._finish_tailor(results)
    w.registry.record_resume.assert_called_once_with("1", str(tmp_path / "1"))   # success only
    assert "1 of 2" in shown["text"] and "boom" in shown["text"]                 # failure surfaced
    assert w._tailoring is False and reloaded == [1]


def test_finish_tailor_opens_folder_only_when_enabled(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    monkeypatch.setattr(w, "reload_data", lambda: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    opened = []
    monkeypatch.setattr(mw.os, "startfile", lambda p: opened.append(p), raising=False)
    results = [{"id": "1", "label": "Eng @ A", "dir": tmp_path / "1", "error": None}]

    # default OFF (key absent) -> the folder is NOT opened
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    w._finish_tailor(list(results))
    assert opened == []

    # toggled ON -> the last folder opens
    monkeypatch.setattr(mw.settings, "load", lambda: {"tailor_open_folder": True})
    w._finish_tailor(list(results))
    assert opened == [str(tmp_path / "1")]


def test_tailor_warns_only_on_large_batch(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(w, "_apply_auth_env", lambda: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    launched = []
    monkeypatch.setattr(mw.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: launched.append(fn))
    confirms = []
    monkeypatch.setattr(w, "_confirm_large_tailor",
                        lambda n: confirms.append(n) or False)   # user cancels
    monkeypatch.setattr(w, "_job_payload",
                        lambda i: {"job_posting_id": i, "company_name": "C", "job_title": "T"})

    # large batch -> warned, cancelled (nothing launched)
    monkeypatch.setattr(w, "_selected_ids", lambda: [str(i) for i in range(6)])
    w._tailor_selected()
    assert confirms == [6] and launched == []

    # small batch -> no warning, launches
    confirms.clear()
    monkeypatch.setattr(w, "_selected_ids", lambda: ["1", "2", "3"])
    w._tailor_selected()
    assert confirms == [] and len(launched) == 1


def test_run_scraper_dialog_runs_worker(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_confirm_scrape", lambda: "bounded")
    ran = {}

    def fake_run_async(owner, fn, on_done=None, on_error=None):
        ran["fn"] = fn

    monkeypatch.setattr(mw.workers, "run_async", fake_run_async)
    monkeypatch.setattr(w, "_scrape_work", lambda bounded: ran.setdefault("bounded", bounded))
    w._run_scraper_dialog()
    assert "fn" in ran
    ran["fn"]()
    assert ran["bounded"] is True


def test_check_setup_reports_ok(qtbot, monkeypatch):
    w = _win(qtbot)
    from resume_tailor import master_validate
    monkeypatch.setattr(master_validate, "check_setup", lambda: {"master": [], "answers": []})
    monkeypatch.setattr(mw.settings, "load", lambda: {})
    monkeypatch.setattr(mw.settings, "secret_status", lambda: {})
    shown = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.setdefault("info", True)))
    w._check_setup()
    assert shown.get("info")


def test_first_run_hint_visible_without_data(qtbot):
    w = _win(qtbot)   # csv_paths=[] -> no jobs loaded
    assert not w.high_tab._empty_widget.isHidden()
    assert w.high_tab.table.isHidden()


def test_first_run_hint_buttons_navigate(qtbot, monkeypatch):
    w = _win(qtbot)
    ran = []
    monkeypatch.setattr(w, "_run_scraper_dialog", lambda: ran.append("scrape"))
    btns = {b.text(): b for b in w.high_tab._empty_widget.findChildren(QtWidgets.QPushButton)}
    btns["Run scraper"].click()
    assert ran == ["scrape"]
    btns["Open Settings"].click()
    assert w.tabs.currentWidget() is w._tab_widgets["Settings"]
    btns["Set up Resume Data"].click()
    assert w.tabs.currentWidget() is w._tab_widgets["Resume Data"]


def test_export_tracker_writes_via_registry(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    dest = tmp_path / "backup.db"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(dest), "")))
    w._export_tracker()
    w.registry.export_to.assert_called_once_with(dest)


def test_export_tracker_cancel_is_noop(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    w._export_tracker()
    assert not w.registry.export_to.called


def test_import_tracker_merges_and_reloads(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    src = tmp_path / "backup.db"
    src.write_bytes(b"x")
    w.registry.import_from.return_value = {"seen": 3, "status": 2, "resume_paths": 1}
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    info = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: info.setdefault("text", a[2])))
    refreshed = []
    monkeypatch.setattr(w, "_refresh_tracker", lambda: refreshed.append(True))
    w._import_tracker()
    w.registry.import_from.assert_called_once_with(src)
    assert refreshed == [True]
    assert "3" in info["text"]  # counts surfaced


def test_import_tracker_decline_is_noop(qtbot, monkeypatch, tmp_path):
    w = _win(qtbot)
    src = tmp_path / "backup.db"
    src.write_bytes(b"x")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    w._import_tracker()
    assert not w.registry.import_from.called
