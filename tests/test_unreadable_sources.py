"""3.12: an unreadable source file must be named, not silently reported as "no jobs".

Two defects lived here, both found by pointing the real dashboard at a truncated
``linkedin_jobs_master.csv.gz`` in a clean-clone sandbox rather than by reading
the handler:

1. ``jobsdata.load_files`` caught ``(OSError, ValueError)``. A half-written gzip
   raises ``zlib.error`` and a cut-short one raises ``EOFError``, and neither is
   either of those, so the exception escaped the per-file skip and unwound the
   whole loop. One corrupt Drive master therefore stopped the local scrape files
   and manual adds from loading too — the exact opposite of what the loop is for.

2. Even when a file *was* skipped, nothing said so. An empty frame rendered the
   first-run panel, so a user with a 37 MB master mid-sync was told "No jobs yet"
   and offered to set the app up from scratch.

``load_files`` now takes an optional ``problems`` list, and the empty panel swaps
to wording that names the file and the parser's own reason.
"""
import gzip
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

import jobsdata
from qt.main_window import MainWindow


def _good_gz(path: Path, jid: str = "1") -> Path:
    df = pd.DataFrame({"job_posting_id": [jid], "job_title": ["Analyst"],
                       "company_name": ["Acme"], "url": ["https://x/1"]})
    df.to_csv(path, index=False, compression="gzip")
    return path


def _truncated_gz(path: Path) -> Path:
    """A gzip whose deflate stream is cut mid-block: raises zlib.error, not OSError."""
    payload = "job_posting_id,job_title\n" + "".join(f"{i},Analyst {i}\n" for i in range(500))
    whole = gzip.compress(payload.encode("utf-8"))
    path.write_bytes(whole[: len(whole) // 2])
    return path


# ---- load_files --------------------------------------------------------------


def test_a_truncated_gz_raises_something_that_is_not_oserror_or_valueerror(tmp_path):
    """The premise of the fix. If this ever stops holding, the narrow catch was fine."""
    from csv_io import read_csv_gz
    bad = _truncated_gz(tmp_path / "linkedin_jobs_master.csv.gz")
    with pytest.raises(Exception) as ei:
        read_csv_gz(bad)
    assert not isinstance(ei.value, (OSError, ValueError)), type(ei.value)


def test_one_unreadable_source_does_not_stop_the_others_loading(tmp_path):
    bad = _truncated_gz(tmp_path / "linkedin_jobs_master.csv.gz")
    good = _good_gz(tmp_path / "local_rows.csv.gz", jid="42")
    problems: list[tuple[Path, str]] = []
    df, id_to_path = jobsdata.load_files([bad, good], problems=problems)
    assert list(df["job_posting_id"]) == ["42"]
    assert [p for p, _ in problems] == [bad]
    assert problems[0][1]


def test_a_source_missing_the_id_column_is_reported_not_just_dropped(tmp_path):
    odd = tmp_path / "wrong_schema.csv.gz"
    pd.DataFrame({"title": ["x"]}).to_csv(odd, index=False, compression="gzip")
    problems: list[tuple[Path, str]] = []
    df, _ = jobsdata.load_files([odd], problems=problems)
    assert df.empty
    assert problems == [(odd, "no job_posting_id column")]


def test_an_absent_path_is_not_a_problem(tmp_path):
    """A file that is simply not there is the normal first run, not a failure."""
    problems: list[tuple[Path, str]] = []
    df, _ = jobsdata.load_files([tmp_path / "nothing.csv.gz"], problems=problems)
    assert df.empty and problems == []


def test_problems_is_optional_so_every_existing_caller_is_unchanged(tmp_path):
    bad = _truncated_gz(tmp_path / "m.csv.gz")
    df, id_to_path = jobsdata.load_files([bad])
    assert df.empty and id_to_path == {}


# ---- what the user is shown --------------------------------------------------


def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    return reg


@pytest.fixture
def win(qtbot):
    w = MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def test_the_empty_panel_says_first_run_when_nothing_is_wrong(win):
    win._load_problems = ()
    win._refresh_empty_hint()
    assert win._empty_title.text() == MainWindow.EMPTY_FIRST_RUN[0]
    assert "Three steps" in win._empty_msg.text()


def test_the_empty_panel_names_the_file_and_the_reason(win, tmp_path):
    bad = tmp_path / "linkedin_jobs_master.csv.gz"
    win._load_problems = ((bad, "zlib.error: invalid stored block lengths"),)
    win._refresh_empty_hint()
    assert win._empty_title.text() == MainWindow.EMPTY_UNREADABLE_TITLE
    body = win._empty_msg.text()
    assert "linkedin_jobs_master.csv.gz" in body
    assert "invalid stored block lengths" in body
    assert "Refresh" in body                       # says what to do
    assert "Three steps" not in body               # and does not claim a fresh install


def test_the_message_caps_the_list_but_states_the_true_count(win, tmp_path):
    win._load_problems = tuple((tmp_path / f"f{i}.csv.gz", "boom") for i in range(6))
    body = win.unreadable_sources_message()
    assert body.startswith("6 job file(s)")
    assert "and 3 more" in body
    assert "f4.csv.gz" not in body


def test_a_partial_failure_reaches_the_status_line(win):
    """With rows loaded the empty panel never shows, so the count has to go here."""
    win.df = pd.DataFrame({"job_posting_id": ["1"], "is_seen": ["no"]})
    win.df_high = win.df
    win._load_problems = ((Path("a.csv.gz"), "boom"),)
    assert "1 source file(s) unreadable" in win._summary_line()
    win._load_problems = ()
    assert "unreadable" not in win._summary_line()


def test_apply_frames_carries_the_problems_through(win, tmp_path):
    from qt.main_window import LoadedFrames
    bad = tmp_path / "m.csv.gz"
    loaded = LoadedFrames(pd.DataFrame(), {}, None, 36, ((bad, "zlib.error: x"),))
    win._apply_frames(loaded)
    assert win._empty_title.text() == MainWindow.EMPTY_UNREADABLE_TITLE
    # and a clean reload clears it again
    win._apply_frames(LoadedFrames(pd.DataFrame(), {}, None, 36, ()))
    assert win._empty_title.text() == MainWindow.EMPTY_FIRST_RUN[0]


def test_a_bare_tuple_from_an_older_caller_still_applies(win):
    """_apply_frames documents that it accepts (df, id_to_path); keep that true."""
    win._apply_frames((pd.DataFrame(), {}))
    assert win._load_problems == ()
