"""SP4: score preview visibility + the worker-backed job actions (mocked backends)."""
from unittest.mock import MagicMock

import pandas as pd
import pytest
from PySide6 import QtWidgets

from qt import main_window as mw
from qt.main_window import PREVIEW_TABS, TAB_TITLES, MainWindow


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = set()
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
