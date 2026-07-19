"""Master-CSV write serialization (audit P2-25/P2-28): the dashboard deletes on a
background queue while manual-add runs on a worker thread, so the local master's
read-modify-write helpers must hold one shared lock or a concurrent pair can both
read at N rows and last-writer-win, silently dropping a row.
"""
import logging
import sys
import threading
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


def _master_ids(path: Path) -> set[str]:
    return set(pd.read_csv(path, dtype={"job_posting_id": str})["job_posting_id"])


def _rec(jid: str) -> dict:
    return {"job_posting_id": jid, "job_title": f"T{jid}", "company_name": "Acme",
            "score": "5", "source": "manual"}


def test_master_writes_hold_the_shared_lock(tmp_path):
    """While another thread holds the master-write lock, an append must block
    instead of racing the read-modify-write."""
    master = tmp_path / "linkedin_jobs_master.csv"
    done = threading.Event()

    lock = jobsdata._MASTER_WRITE_LOCK
    with lock:
        t = threading.Thread(
            target=lambda: (jobsdata.append_manual_job(_rec("a1"), master_csv=master),
                            done.set()))
        t.start()
        # the append must NOT complete while we hold the lock
        assert not done.wait(0.3)
    assert done.wait(5), "append never finished after the lock was released"
    t.join(5)
    assert _master_ids(master) == {"a1"}


def test_concurrent_append_and_delete_lose_no_row(tmp_path, monkeypatch):
    """Interleaved appends + deletes over one master end with exactly the expected
    rows — no lost update (audit P2-25)."""
    master = tmp_path / "linkedin_jobs_master.csv"
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)   # isolate config.json
    # seed rows the deleter will target
    for jid in ("d1", "d2"):
        jobsdata.append_manual_job(_rec(jid), master_csv=master)

    keep_ids = [f"k{i}" for i in range(6)]
    errors: list[BaseException] = []

    def appender():
        try:
            for jid in keep_ids:
                jobsdata.append_manual_job(_rec(jid), master_csv=master)
                time.sleep(0.01)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def deleter():
        try:
            jobsdata.delete_jobs(["d1"], master_csv=master)
            time.sleep(0.01)
            jobsdata.delete_jobs(["d2"], master_csv=master)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ta, td = threading.Thread(target=appender), threading.Thread(target=deleter)
    ta.start(); td.start(); ta.join(30); td.join(30)
    assert not errors
    assert _master_ids(master) == set(keep_ids)


def test_manual_gz_append_failure_is_logged_not_silent(tmp_path, caplog, monkeypatch):
    """A failed manual-gz convenience append must WARN (master/gz divergence was
    previously swallowed with a bare `pass` — audit P2-28)."""
    master = tmp_path / "linkedin_jobs_master.csv"
    real = jobsdata._append_dedup_csv

    def failing(record, path, *, compression=None):
        if str(path).endswith(".gz"):
            raise OSError("disk full")
        return real(record, path, compression=compression)

    monkeypatch.setattr(jobsdata, "_append_dedup_csv", failing)
    with caplog.at_level(logging.WARNING):
        added = jobsdata.append_manual_job(_rec("g1"), master_csv=master)
    assert added is True                      # the canonical master append still lands
    assert any("manual" in r.message.lower() and "gz" in r.message.lower()
               for r in caplog.records), "gz failure must be logged, not swallowed"
