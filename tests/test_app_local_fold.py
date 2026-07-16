"""app.py owns the local-runs fold: EVERY entry point (watcher argv, shortcut,
direct run) sees local scrape/manual files, and open_dashboard no longer double-adds."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import app  # noqa: E402
import jobsdata  # noqa: E402


def _load_open_dashboard():
    """Import local/open_dashboard.pyw (a .pyw is invisible to plain `import`)."""
    path = REPO / "local" / "open_dashboard.pyw"
    spec = importlib.util.spec_from_file_location("open_dashboard", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["open_dashboard"] = mod
    spec.loader.exec_module(mod)  # top level is imports only — no Qt, no side effects
    return mod


def test_with_local_runs_appends_missing(monkeypatch, tmp_path):
    drive_master = tmp_path / "linkedin_jobs_master.csv.gz"
    local_a = tmp_path / "evening" / "a_scored.csv.gz"
    monkeypatch.setattr(jobsdata, "local_run_files", lambda base=None: [local_a])
    got = app._with_local_runs([drive_master])
    assert got == [drive_master, local_a]


def test_with_local_runs_no_duplicates(monkeypatch, tmp_path):
    local_a = tmp_path / "evening" / "a_scored.csv.gz"
    monkeypatch.setattr(jobsdata, "local_run_files", lambda base=None: [local_a])
    got = app._with_local_runs([local_a])
    assert got == [local_a]


def test_with_local_runs_survives_failure(monkeypatch):
    monkeypatch.setattr(jobsdata, "local_run_files",
                        lambda base=None: (_ for _ in ()).throw(OSError("disk")))
    assert app._with_local_runs([Path("x.csv")]) == [Path("x.csv")]


def test_open_dashboard_no_longer_folds():
    src = (REPO / "local" / "open_dashboard.pyw").read_text(encoding="utf-8")
    # The fold has exactly one owner now (app.main). _resolve_sources may still CHECK
    # local_run_files for its no-Drive fallback, but must not append them to sources.
    assert "*local" not in src and ", *local" not in src


def test_open_dashboard_no_drive_but_local_runs_returns_empty_ok(monkeypatch):
    open_dashboard = _load_open_dashboard()
    import watcher
    monkeypatch.setattr(watcher, "load_config", lambda: {})
    monkeypatch.setattr(watcher, "detect_gdrive_root", lambda: None)
    monkeypatch.setattr(jobsdata, "local_run_files",
                        lambda base=None: [Path("evening/a_scored.csv.gz")])
    sources, err = open_dashboard._resolve_sources()
    assert err is None and sources == []   # app.main folds the local files in


def test_open_dashboard_no_drive_no_local_opens_get_started(monkeypatch):
    """A fresh install (no Drive folder, no data) must still open the window —
    app shows the get-started panel — never an error popup (README's promise)."""
    open_dashboard = _load_open_dashboard()
    import watcher
    monkeypatch.setattr(watcher, "load_config", lambda: {})
    monkeypatch.setattr(watcher, "detect_gdrive_root", lambda: None)
    sources, err = open_dashboard._resolve_sources()
    assert sources == [] and err is None
