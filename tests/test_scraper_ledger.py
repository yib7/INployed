"""The collection ledger: the skip list windows on the LAST day a job was collected.

The master keeps the FIRST-found extracted_date of a job (append_to_master is
"existing wins"), so a job first found three weeks ago and collected again
yesterday looked three weeks old to the skip-list window and was left off it.
Bright Data then collected and billed it again on every run. The ledger
(collected_ids_ledger.csv) records every id each run got back with that run's
date, newest date per id, and load_exclude_ids windows on the newest date any
source knows.
"""
import asyncio
import gzip
import json
import os

import pandas as pd
import pytest

import scraper


def _days_ago(n: int) -> str:
    return (pd.Timestamp.now() - pd.Timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Every scraper data path in tmp_path, no Drive master, no env overrides."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    return tmp_path


def _write_master(path, rows):
    path.write_text("job_posting_id,extracted_date\n"
                    + "".join(f"{jid},{dt}\n" for jid, dt in rows), encoding="utf-8")
    return path


def _write_ledger(sandbox, rows):
    return _write_ledger_file(sandbox / "collected_ids_ledger.csv", rows)


def _write_ledger_file(path, rows):
    path.write_text("job_posting_id,collected_date\n"
                    + "".join(f"{jid},{dt}\n" for jid, dt in rows), encoding="utf-8")
    return path


def _ledger(sandbox) -> dict:
    df = pd.read_csv(sandbox / "collected_ids_ledger.csv", dtype=str)
    assert list(df.columns) == ["job_posting_id", "collected_date"]
    return dict(zip(df["job_posting_id"], df["collected_date"]))


# --- the bug -----------------------------------------------------------------

def test_a_job_first_found_20_days_ago_but_collected_yesterday_is_skipped(
        sandbox, monkeypatch):
    """THE regression: window 14, first found 20 days ago, collected yesterday."""
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")
    _write_master(sandbox / "linkedin_jobs_master.csv", [("job", _days_ago(20))])
    assert "job" not in scraper.load_exclude_ids()          # the premise: master date alone
    _write_ledger(sandbox, [("job", _days_ago(1))])
    assert "job" in scraper.load_exclude_ids()


def test_a_newer_drive_master_date_also_counts(sandbox, monkeypatch, tmp_path):
    """The effective date is the newest ANY source knows, the Drive master too."""
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")
    _write_master(sandbox / "linkedin_jobs_master.csv", [("job", _days_ago(20))])
    drive = _write_master(tmp_path / "drive_master.csv", [("job", _days_ago(2))])
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    assert "job" in scraper.load_exclude_ids()


def test_a_ledger_only_id_is_in_the_skip_list(sandbox):
    """An id the run collected but no master holds (a company on the blocklist, or
    a master write that failed) was still billed; it must not be billed twice."""
    _write_ledger(sandbox, [("only_in_ledger", _days_ago(3))])
    assert scraper.load_exclude_ids() == ["only_in_ledger"]


def test_a_stale_ledger_date_is_windowed_out(sandbox, monkeypatch):
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")
    _write_ledger(sandbox, [("old", _days_ago(30)), ("new", _days_ago(2))])
    assert scraper.load_exclude_ids() == ["new"]


def test_the_skip_list_is_ordered_by_the_newest_date_any_source_knows(sandbox):
    """cap_exclude_ids keeps the tail, so a job collected again yesterday has to
    sort as yesterday's, whatever its first-found date in the master says."""
    _write_master(sandbox / "linkedin_jobs_master.csv",
                  [("a", _days_ago(10)), ("b", _days_ago(5)), ("c", _days_ago(7))])
    _write_ledger(sandbox, [("a", _days_ago(1))])
    assert scraper.load_exclude_ids() == ["c", "b", "a"]


def test_a_recent_drive_id_outranks_an_older_own_id(sandbox, monkeypatch, tmp_path):
    """Newest-first across every source: a job the VM collected yesterday can
    resurface in a 'Past 24 hours' search exactly as one of ours can."""
    _write_master(sandbox / "linkedin_jobs_master.csv", [("own_old", _days_ago(9))])
    drive = _write_master(tmp_path / "drive_master.csv", [("vm_new", _days_ago(1))])
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    assert scraper.load_exclude_ids() == ["own_old", "vm_new"]


def test_undated_ids_still_sort_first(sandbox):
    """External ids carry no date; a date we cannot prove must not outrank one we can."""
    (sandbox / "external_exclude_ids.json").write_text(json.dumps(["ext"]), encoding="utf-8")
    _write_ledger(sandbox, [("dated", _days_ago(2))])
    assert scraper.load_exclude_ids() == ["ext", "dated"]


# --- recording ---------------------------------------------------------------

def test_recording_keeps_the_newest_date_per_id(sandbox):
    scraper.record_collected_ids(["a", "b"], _days_ago(5))
    scraper.record_collected_ids(["a"], _days_ago(1))
    scraper.record_collected_ids(["b"], _days_ago(9))        # an older date never wins
    assert _ledger(sandbox) == {"a": _days_ago(1), "b": _days_ago(5)}


def test_recording_prunes_rows_older_than_60_days(sandbox):
    _write_ledger(sandbox, [("stale", _days_ago(61)), ("kept", _days_ago(59))])
    scraper.record_collected_ids(["fresh"], _days_ago(0))
    assert _ledger(sandbox) == {"kept": _days_ago(59), "fresh": _days_ago(0)}


def test_recording_writes_atomically_and_never_crashes_the_run(sandbox, monkeypatch, capsys):
    """The jobs are already collected and saved by the time this runs; a ledger
    write failure must cost a warning, not the run."""
    _write_ledger(sandbox, [("a", _days_ago(2))])
    before = (sandbox / "collected_ids_ledger.csv").read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    scraper.record_collected_ids(["b"], _days_ago(0))        # must not raise
    assert (sandbox / "collected_ids_ledger.csv").read_bytes() == before
    assert [p.name for p in sandbox.iterdir() if p.suffix == ".tmp"] == []
    out = capsys.readouterr().out
    assert "WARNING:" in out and "billed again" in out


def test_a_corrupt_ledger_warns_and_is_rebuilt(sandbox, capsys):
    (sandbox / "collected_ids_ledger.csv").write_text("not,a\nledger,at all\n",
                                                      encoding="utf-8")
    assert scraper.load_exclude_ids() == []                  # no crash, no ids invented
    out = capsys.readouterr().out
    assert "WARNING:" in out and "collected_ids_ledger.csv" in out
    scraper.record_collected_ids(["a"], _days_ago(0))
    assert _ledger(sandbox) == {"a": _days_ago(0)}
    assert "WARNING:" in capsys.readouterr().out


def test_an_unreadable_ledger_does_not_crash_the_skip_list(sandbox, monkeypatch, capsys):
    _write_master(sandbox / "linkedin_jobs_master.csv", [("m", _days_ago(1))])
    _write_ledger(sandbox, [("a", _days_ago(1))])
    real = pd.read_csv

    def locked(path, *a, **k):
        if str(path).endswith("collected_ids_ledger.csv"):
            raise PermissionError("locked by another program")
        return real(path, *a, **k)

    monkeypatch.setattr(pd, "read_csv", locked)
    assert scraper.load_exclude_ids() == ["m"]
    assert "WARNING:" in capsys.readouterr().out


# --- the one-time seed from saved run files -----------------------------------

def _run_csv(sandbox, label, date, rows, extracted=True):
    d = sandbox / label
    d.mkdir(exist_ok=True)
    p = d / f"linkedin_jobs_{date}_{label}.csv"
    if extracted:
        body = "job_posting_id,job_title,extracted_date\n" + "".join(
            f"{jid},t,{dt}\n" for jid, dt in rows)
    else:
        body = "job_posting_id,job_title\n" + "".join(f"{jid},t\n" for jid, _ in rows)
    p.write_text(body, encoding="utf-8")
    return p


def test_a_missing_ledger_is_seeded_from_the_saved_run_files(sandbox, monkeypatch):
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")
    _write_master(sandbox / "linkedin_jobs_master.csv", [("job", _days_ago(20))])
    _run_csv(sandbox, "morning", _days_ago(2), [("job", _days_ago(2))])
    assert "job" in scraper.load_exclude_ids()


def test_the_seed_reads_scored_files_and_falls_back_to_the_filename_date(sandbox):
    label = scraper.RUN_LABELS[1]
    _run_csv(sandbox, label, _days_ago(4), [("raw", "")], extracted=False)
    scored = sandbox / label / f"linkedin_jobs_{_days_ago(3)}_{label}_scored.csv.gz"
    with gzip.open(scored, "wt", encoding="utf-8") as f:
        f.write(f"job_posting_id,extracted_date\nscored,{_days_ago(3)}\n")
    seeded = scraper.seed_ledger_from_run_files()
    assert dict(zip(seeded["job_posting_id"], seeded["collected_date"])) == {
        "raw": _days_ago(4), "scored": _days_ago(3)}


def test_the_seed_skips_run_files_older_than_the_ledger_keeps(sandbox):
    _run_csv(sandbox, "evening", _days_ago(90), [("ancient", _days_ago(90))])
    _run_csv(sandbox, "evening", _days_ago(1), [("recent", _days_ago(1))])
    seeded = scraper.seed_ledger_from_run_files()
    assert seeded["job_posting_id"].tolist() == ["recent"]


def test_the_first_recording_persists_the_seed(sandbox):
    _run_csv(sandbox, "night", _days_ago(6), [("old_run", _days_ago(6))])
    scraper.record_collected_ids(["today"], _days_ago(0))
    assert _ledger(sandbox) == {"old_run": _days_ago(6), "today": _days_ago(0)}


def test_an_unreadable_run_file_is_skipped_with_a_warning(sandbox, capsys):
    d = sandbox / "morning"
    d.mkdir()
    bad = d / f"linkedin_jobs_{_days_ago(1)}_morning_scored.csv.gz"
    bad.write_bytes(b"\x1f\x8b truncated gzip")
    _run_csv(sandbox, "evening", _days_ago(1), [("good", _days_ago(1))])
    seeded = scraper.seed_ledger_from_run_files()
    assert seeded["job_posting_id"].tolist() == ["good"]
    assert "WARNING:" in capsys.readouterr().out


# --- main() records every id it got back, known and new -----------------------

def test_a_run_records_every_collected_id_known_and_new(sandbox, monkeypatch):
    label = scraper.RUN_LABELS[0]
    _write_master(sandbox / "linkedin_jobs_master.csv", [("1", _days_ago(30))])
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    monkeypatch.setattr(scraper, "load_blocklist", lambda: ["blockedco"])

    async def fake_download(session, snapshot_id):
        return [{"job_posting_id": "1", "company_name": "Acme"},       # known
                {"job_posting_id": "2", "company_name": "Acme"},       # new
                {"job_posting_id": "2", "company_name": "Acme"},       # duplicate
                {"job_posting_id": "3", "company_name": "BlockedCo"}]  # billed, then dropped

    monkeypatch.setattr(scraper, "download", fake_download)
    asyncio.run(scraper.main(snapshot_id="snap", run_label=label))
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    assert _ledger(sandbox) == {"1": today, "2": today, "3": today}
