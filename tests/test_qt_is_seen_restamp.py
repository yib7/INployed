"""Marking jobs seen does not make the next load re-store the whole master.

`_write_is_seen` rewrites a source file (usually the Drive master) changing only
is_seen. That changes the file's (size, mtime_ns) stamp, so the description
cache treated the master as new and the next load read and stored all of it
again (~1-1.7 s, on every startup after marking jobs seen). The writer now moves
the cache's stamp to the rewritten file, compare-and-set, and only when nothing
else touched the file while it was being read.
"""
import os
from pathlib import Path

import pandas as pd
import pytest

import desc_cache
import jobsdata
from qt import mw_tracker
from qt.mw_tracker import _TrackerActions


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "MIRROR_DIR", tmp_path / "mirror")
    monkeypatch.setattr(jobsdata, "load_removed_jobs", lambda: set())
    return tmp_path


def _master(env) -> Path:
    path = env / "drive" / "LinkedInJobs" / "linkedin_jobs_master.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"job_posting_id": "1", "job_title": "Analyst", "is_seen": "no",
         "job_summary": "summary one", "job_description_formatted": "<p>one</p>"},
        {"job_posting_id": "2", "job_title": "Engineer", "is_seen": "no",
         "job_summary": "summary two", "job_description_formatted": None},
    ]).to_csv(path, index=False, compression="gzip")
    return path


def _full_reads(monkeypatch) -> list:
    calls: list = []
    real = jobsdata.read_csv_gz

    def spy(path, *a, **k):
        if k.get("usecols") is None:
            calls.append(Path(path).name)
        return real(path, *a, **k)
    monkeypatch.setattr(jobsdata, "read_csv_gz", spy)
    return calls


def _write_is_seen(ids, value, paths):
    _TrackerActions._write_is_seen(None, ids, value, paths)


def test_marking_seen_keeps_the_masters_cache_current(env, monkeypatch):
    master = _master(env)
    _, id_to_path = jobsdata.load_files([master], descriptions_cached=True)
    before = master.stat()
    _write_is_seen(["1"], "yes", {id_to_path["1"]})
    assert (master.stat().st_size, master.stat().st_mtime_ns) != \
        (before.st_size, before.st_mtime_ns)
    assert desc_cache.is_current(str(master), master)

    reads = _full_reads(monkeypatch)
    df, _ = jobsdata.load_files([master], descriptions_cached=True)
    assert reads == []                                   # no full re-read + store
    assert df.set_index("job_posting_id").loc["1", "is_seen"] == "yes"
    assert jobsdata.job_description("1", str(master)) == {
        "job_summary": "summary one", "job_description_formatted": "<p>one</p>"}


def test_a_file_changed_during_the_read_is_not_restamped(env, monkeypatch):
    master = _master(env)
    _, id_to_path = jobsdata.load_files([master], descriptions_cached=True)
    real = mw_tracker.read_csv_gz

    def read_then_someone_writes(path, *a, **k):
        df = real(path, *a, **k)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
        return df
    monkeypatch.setattr(mw_tracker, "read_csv_gz", read_then_someone_writes)
    _write_is_seen(["1"], "yes", {id_to_path["1"]})
    assert not desc_cache.is_current(str(master), master)


def test_a_cache_that_was_already_stale_stays_stale(env):
    master = _master(env)
    jobsdata.load_files([master], descriptions_cached=True)
    st = master.stat()
    os.utime(master, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))  # synced since
    _write_is_seen(["1"], "yes", {master})
    assert not desc_cache.is_current(str(master), master)


def test_a_failing_restamp_is_silent(env, monkeypatch):
    master = _master(env)
    jobsdata.load_files([master], descriptions_cached=True)

    def boom(*a, **k):
        raise RuntimeError("cache gone")
    monkeypatch.setattr(desc_cache, "restamp", boom)
    _write_is_seen(["1"], "yes", {master})                # does not raise
    assert jobsdata.load_files([master])[0].set_index(
        "job_posting_id").loc["1", "is_seen"] == "yes"
