"""Run-scraper button (local/ui.py) — spend-guarded, pure logic, no widgets.

The button confirms first (Small test run / Full run / Cancel) because a scrape
costs real Bright Data money. Cancel must spawn nothing; a confirmed run spawns
scraper.py (then score_jobs.py) on a background thread. Tests mock subprocess —
no real scrape ever runs.
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import ui  # noqa: E402


class _Proc:
    def __init__(self, rc=0):
        self._rc = rc

    def wait(self):
        return self._rc


def _fake():
    f = types.SimpleNamespace()
    f._scraping = False
    f._set_status = lambda m: None
    f._log_error = lambda c, e: None
    f.reload_data = lambda: None
    f.root = types.SimpleNamespace(after=lambda ms, fn: fn())
    f._scraper_cmd = ui.App._scraper_cmd
    f._scorer_cmd = ui.App._scorer_cmd
    f._scrape_worker = ui.App._scrape_worker.__get__(f)
    return f


def test_scraper_cmd_bounded_and_full():
    bounded = ui.App._scraper_cmd(True)
    assert "scraper.py" in bounded
    assert "--max-keywords" in bounded and "1" in bounded
    assert "--limit" in bounded and "5" in bounded
    full = ui.App._scraper_cmd(False)
    assert "scraper.py" in full and "--max-keywords" not in full


def test_run_scraper_dialog_cancel_starts_no_thread(monkeypatch):
    f = _fake()
    f._confirm_scrape = lambda: None
    started = []
    monkeypatch.setattr(ui.threading, "Thread",
                        lambda *a, **k: started.append((a, k)) or types.SimpleNamespace(
                            start=lambda: None, daemon=True))
    ui.App._run_scraper_dialog(f)
    assert started == []  # nothing spawned on cancel


def test_run_scraper_dialog_bounded_starts_worker_thread(monkeypatch):
    f = _fake()
    f._confirm_scrape = lambda: "bounded"
    captured = {}

    def _thread(target=None, args=(), daemon=None):
        captured["target"] = target
        captured["args"] = args
        return types.SimpleNamespace(start=lambda: None, daemon=daemon)

    monkeypatch.setattr(ui.threading, "Thread", _thread)
    ui.App._run_scraper_dialog(f)
    assert captured["args"] == (True,)  # bounded -> _scrape_worker(True)


def test_scrape_worker_spawns_scraper_then_scorer(monkeypatch):
    f = _fake()
    seen = []
    monkeypatch.setattr(ui.subprocess, "Popen",
                        lambda args, *a, **k: seen.append(args) or _Proc(0))
    ui.App._scrape_worker(f, True)
    assert len(seen) == 2
    assert "scraper.py" in seen[0] and "--max-keywords" in seen[0]
    assert "score_jobs.py" in seen[1]
    assert f._scraping is False  # cleared in finally
