"""The dashboard keeps showing the Drive master while Google Drive is not running.

When Google Drive for desktop stops, the whole ``D:`` drive and the master on it
are gone. ``load_files`` skips an absent path without a word (the normal first
run), so the full master's jobs would quietly shrink to the local ones with
nothing on screen to say why.

Every clean read of a source outside the repo refreshes a local copy, and a
source whose FOLDER is gone loads from that copy, with a banner naming the file,
the copy's age and the fix. A folder that exists without the master is a fresh
setup (or a master the user removed), so the copy never brings that back.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import jobsdata
from qt.main_window import LoadedFrames, MainWindow


def _write_master(path: Path, ids=("1", "2", "3")) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"job_posting_id": list(ids), "job_title": ["Analyst"] * len(ids),
                  "company_name": ["Acme"] * len(ids), "is_seen": ["no"] * len(ids)}
                 ).to_csv(path, index=False, compression="gzip")
    return path


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A Drive-like folder outside the repo, with its own mirror dir."""
    monkeypatch.setattr(jobsdata, "MIRROR_DIR", tmp_path / "mirror")
    return tmp_path / "drive" / "LinkedInJobs" / "linkedin_jobs_master.csv.gz"


def _unmount(master: Path) -> None:
    """Google Drive stopped: the folder (and everything under it) is gone."""
    for f in master.parent.iterdir():
        f.unlink()
    master.parent.rmdir()


# ---- the copy ------------------------------------------------------------------


def test_a_clean_read_saves_a_local_copy(drive):
    _write_master(drive)
    df, _ = jobsdata.load_files([drive])
    copy = jobsdata.mirror_path(drive)
    assert len(df) == 3
    assert copy.read_bytes() == drive.read_bytes()
    assert copy.stat().st_mtime_ns == drive.stat().st_mtime_ns


def test_the_copy_is_keyed_by_folder_and_file_so_a_new_drive_letter_finds_it(drive):
    other_letter = Path("G:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    assert jobsdata.mirror_path(other_letter) == jobsdata.mirror_path(drive)


def test_an_unchanged_source_is_not_copied_again(drive, monkeypatch):
    _write_master(drive)
    jobsdata.load_files([drive])
    calls = []
    monkeypatch.setattr(jobsdata.shutil, "copy2", lambda *a, **k: calls.append(a))
    jobsdata.load_files([drive])
    assert calls == []


def test_an_unreadable_source_never_replaces_a_good_copy(drive):
    _write_master(drive)
    jobsdata.load_files([drive])
    good = jobsdata.mirror_path(drive).read_bytes()
    drive.write_bytes(b"not a gzip file at all")
    problems = []
    jobsdata.load_files([drive], problems=problems)
    assert [p for p, _ in problems] == [drive]
    assert jobsdata.mirror_path(drive).read_bytes() == good


def test_a_source_inside_the_repo_is_not_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "MIRROR_DIR", tmp_path / "mirror")
    monkeypatch.setattr(jobsdata, "REPO_ROOT", tmp_path / "repo")
    local = _write_master(tmp_path / "repo" / "manual" / "manual_jobs_scored.csv.gz")
    jobsdata.load_files([local])
    assert not (tmp_path / "mirror").exists()


# ---- Drive not running -----------------------------------------------------------


def test_with_drive_gone_the_copy_loads_and_writes_still_target_drive(drive):
    _write_master(drive)
    jobsdata.load_files([drive])
    saved_at = jobsdata.mirror_path(drive).stat().st_mtime
    _unmount(drive)

    offline = []
    df, id_to_path = jobsdata.load_files([drive], offline=offline)
    assert sorted(df["job_posting_id"]) == ["1", "2", "3"]
    assert set(id_to_path.values()) == {drive}          # is_seen writes go to Drive
    assert set(df["_source"]) == {str(drive)}
    assert offline == [(drive, saved_at, 3)]


def test_the_copy_joins_the_local_runs(drive, tmp_path):
    _write_master(drive, ids=("1", "2"))
    local = _write_master(tmp_path / "morning" / "run_scored.csv.gz", ids=("2", "9"))
    jobsdata.load_files([drive, local])
    _unmount(drive)
    df, _ = jobsdata.load_files([drive, local], offline=[])
    assert sorted(df["job_posting_id"]) == ["1", "2", "9"]


def test_with_drive_gone_and_no_copy_the_source_is_reported(drive):
    offline = []
    df, _ = jobsdata.load_files([drive], offline=offline)
    assert df.empty
    assert offline == [(drive, None, 0)]


def test_a_folder_without_the_master_is_a_fresh_setup_and_the_copy_stays_out(drive):
    _write_master(drive)
    jobsdata.load_files([drive])
    drive.unlink()                                        # folder still there
    offline, problems = [], []
    df, _ = jobsdata.load_files([drive], problems=problems, offline=offline)
    assert df.empty and offline == [] and problems == []


def test_a_broken_copy_counts_as_no_copy(drive):
    jobsdata.MIRROR_DIR.mkdir(parents=True)
    jobsdata.mirror_path(drive).write_bytes(b"garbage")
    offline = []
    df, _ = jobsdata.load_files([drive], offline=offline)
    assert df.empty and offline == [(drive, None, 0)]


def test_offline_is_optional(drive):
    df, _ = jobsdata.load_files([drive])
    assert df.empty


# ---- Start Google Drive ------------------------------------------------------------


def test_find_google_drive_app_picks_the_newest_version(tmp_path, monkeypatch):
    base = tmp_path / "Google" / "Drive File Stream"
    for ver in ("99.0.1.0", "131.0.2.0", "131.0.10.0"):
        (base / ver).mkdir(parents=True)
        (base / ver / "GoogleDriveFS.exe").write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr(jobsdata.os, "name", "nt")
    assert jobsdata.find_google_drive_app() == base / "131.0.10.0" / "GoogleDriveFS.exe"


def test_find_google_drive_app_is_none_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr(jobsdata.os, "name", "nt")
    assert jobsdata.find_google_drive_app() is None


# ---- what the user sees ------------------------------------------------------------


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    reg.all_ids.return_value = set()
    return reg


@pytest.fixture
def win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def _frames(df, offline):
    return LoadedFrames(df, {}, None, 36, (), offline)


def test_the_banner_names_the_file_the_copy_age_and_the_fix(win):
    master = Path("D:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    df = pd.DataFrame({"job_posting_id": ["1"], "is_seen": ["no"]})
    win._apply_frames(_frames(df, ((master, 1_757_971_440.0, 15_753),)))
    assert not win.offline_banner.isHidden()
    text = win.offline_label.text()
    assert "Google Drive" in text
    assert "linkedin_jobs_master.csv.gz" in text
    assert "15,753 jobs" in text
    assert "by itself" in text
    assert "My Drive" not in text                         # names the file, not where it lives
    assert "Google Drive offline" in win._summary_line()


def test_the_banner_says_the_jobs_are_hidden_when_there_is_no_copy(win):
    master = Path("D:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    win._apply_frames(_frames(pd.DataFrame(), ((master, None, 0),)))
    assert "hidden" in win.offline_label.text()
    # the empty panel says Drive, and does not offer a fresh setup
    assert win._empty_title.text() == MainWindow.EMPTY_OFFLINE_TITLE
    assert "Three steps" not in win._empty_msg.text()


def test_the_banner_goes_away_when_drive_is_back(win):
    master = Path("D:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    df = pd.DataFrame({"job_posting_id": ["1"], "is_seen": ["no"]})
    win._apply_frames(_frames(df, ((master, 1_757_971_440.0, 1),)))
    win._apply_frames(_frames(df, ()))
    assert win.offline_banner.isHidden()
    assert "offline" not in win._summary_line()


def test_older_five_field_frames_mean_online(win):
    win._apply_frames(LoadedFrames(pd.DataFrame(), {}, None, 36, ()))
    assert win.offline_banner.isHidden()


def test_start_google_drive_button_launches_the_app(win, monkeypatch, tmp_path):
    exe = tmp_path / "GoogleDriveFS.exe"
    launched = []
    monkeypatch.setattr("qt.main_window.find_google_drive_app", lambda: exe)
    monkeypatch.setattr("qt.main_window.os.startfile", lambda p: launched.append(p),
                        raising=False)
    master = Path("D:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    win._apply_frames(_frames(pd.DataFrame(), ((master, None, 0),)))
    assert not win.offline_start_btn.isHidden()
    win.offline_start_btn.click()
    assert launched == [str(exe)]


def test_no_start_button_when_drive_is_not_installed(win, monkeypatch):
    monkeypatch.setattr("qt.main_window.find_google_drive_app", lambda: None)
    master = Path("D:/My Drive/LinkedInJobs/linkedin_jobs_master.csv.gz")
    win._apply_frames(_frames(pd.DataFrame(), ((master, None, 0),)))
    assert win.offline_start_btn.isHidden()


def test_load_frames_passes_the_offline_list_through(win, drive):
    _write_master(drive)
    jobsdata.load_files([drive])
    _unmount(drive)
    win.csv_paths = [drive]
    loaded = win._load_frames()
    assert [p for p, _, _ in loaded.offline] == [drive]
    assert len(loaded.df) == 3


def test_mirror_dir_is_under_the_redirected_test_appdata():
    """conftest points LOCALAPPDATA at a throwaway dir before any import; the copy
    must follow it so the suite never writes into the real app data."""
    assert str(jobsdata.MIRROR_DIR).startswith(os.environ["LOCALAPPDATA"])
