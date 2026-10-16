"""load_files(descriptions_cached=True): the dashboard frame without description text.

The four description columns were ~440 MB of a ~500 MB frame. In this mode each
source's descriptions go to desc_cache once (a cold read), and while the file's
(size, mtime_ns) stamp still matches, later loads read it WITHOUT those columns.
Default mode (every other caller) is unchanged.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import csv_io  # noqa: E402
import desc_cache  # noqa: E402
import jobsdata  # noqa: E402

DESC = list(desc_cache.DESC_COLS)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(desc_cache, "DB_PATH", tmp_path / "appdata" / "descriptions.db")
    monkeypatch.setattr(jobsdata, "MIRROR_DIR", tmp_path / "mirror")
    monkeypatch.setattr(jobsdata, "load_removed_jobs", lambda: set())
    return tmp_path


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
    return path


def _master(env) -> Path:
    return _write(env / "drive" / "LinkedInJobs" / "linkedin_jobs_master.csv.gz", [
        {"job_posting_id": "1", "job_title": "Analyst", "company_name": "Acme",
         "is_seen": "no", "job_summary": "master summary one",
         "job_description_formatted": "<p>formatted one</p>"},
        {"job_posting_id": "2", "job_title": "Engineer", "company_name": "Beta",
         "is_seen": "yes", "job_summary": "master summary two",
         "job_description_formatted": None},
    ])


def _run(env) -> Path:
    return _write(env / "repo" / "daily" / "2026-10-16_scored.csv.gz", [
        {"job_posting_id": "1", "job_title": "Analyst", "company_name": "Acme",
         "is_seen": None, "job_summary": "run summary one",
         "job_description_md": "## run md one", "reason": "good fit"},
        {"job_posting_id": "3", "job_title": "Scientist", "company_name": "Gamma",
         "is_seen": None, "job_summary": "run summary three",
         "job_description_md": "## run md three", "reason": "ok"},
    ])


def _count_reads(monkeypatch) -> list:
    calls: list = []
    real = jobsdata.read_csv_gz

    def spy(path, *a, **k):
        calls.append((Path(path).name, k.get("usecols")))
        return real(path, *a, **k)
    monkeypatch.setattr(jobsdata, "read_csv_gz", spy)
    return calls


def _full_reads(calls) -> list:
    return [name for name, usecols in calls if usecols is None]


# ---- read_csv_gz -----------------------------------------------------------------


def test_read_csv_gz_still_returns_every_column_by_default(env):
    df = csv_io.read_csv_gz(_master(env))
    assert set(DESC) - {"job_description_md", "job_description"} <= set(df.columns)


def test_read_csv_gz_usecols_keeps_the_is_seen_normalisation(env):
    df = csv_io.read_csv_gz(_run(env), usecols=lambda c: c not in DESC)
    assert not set(DESC) & set(df.columns)
    assert df["is_seen"].tolist() == ["no", "no"]


# ---- default mode ----------------------------------------------------------------


def test_default_mode_keeps_the_columns_and_writes_no_cache(env):
    df, _ = jobsdata.load_files([_master(env), _run(env)])
    assert {"job_summary", "job_description_md", "job_description_formatted"} <= set(df.columns)
    assert not desc_cache.DB_PATH.exists()


# ---- cached mode -----------------------------------------------------------------


def test_cold_load_fills_the_cache_and_drops_the_text(env):
    master, run = _master(env), _run(env)
    df, id_to_path = jobsdata.load_files([master, run], descriptions_cached=True)
    assert not set(DESC) & set(df.columns)
    assert sorted(df["job_posting_id"]) == ["1", "2", "3"]
    assert "reason" in df.columns                      # short card text stays
    assert desc_cache.is_current(str(master), master)
    assert desc_cache.is_current(str(run), run)
    assert jobsdata.job_description("2", str(master)) == {"job_summary": "master summary two"}
    assert id_to_path["1"] == master                   # is_seen write target unchanged


def test_warm_load_reads_no_description_text(env, monkeypatch):
    master, run = _master(env), _run(env)
    cold, _ = jobsdata.load_files([master, run], descriptions_cached=True)
    calls = _count_reads(monkeypatch)
    warm, _ = jobsdata.load_files([master, run], descriptions_cached=True)
    assert _full_reads(calls) == []
    assert len(calls) == 2
    assert not set(DESC) & set(warm.columns)
    pd.testing.assert_frame_equal(warm.reset_index(drop=True)[sorted(warm.columns)],
                                  cold.reset_index(drop=True)[sorted(cold.columns)])


def test_a_changed_file_is_read_in_full_again(env, monkeypatch):
    master, run = _master(env), _run(env)
    jobsdata.load_files([master, run], descriptions_cached=True)
    _write(run, [{"job_posting_id": "3", "job_title": "Scientist", "company_name": "Gamma",
                  "is_seen": "no", "job_description_md": "## rewritten md three"}])
    calls = _count_reads(monkeypatch)
    df, _ = jobsdata.load_files([master, run], descriptions_cached=True)
    assert _full_reads(calls) == [run.name]
    assert not set(DESC) & set(df.columns)
    assert jobsdata.job_description("3", str(run)) == {
        "job_description_md": "## rewritten md three"}


def test_the_surviving_rows_source_finds_the_run_files_markdown(env):
    master, run = _master(env), _run(env)
    df, _ = jobsdata.load_files([master, run], descriptions_cached=True)
    row = df[df["job_posting_id"] == "1"].iloc[0]
    assert row["_source"] == str(run)                  # keep="last"
    assert jobsdata.job_description("1", row["_source"]) == {
        "job_summary": "run summary one", "job_description_md": "## run md one"}


def test_warm_read_keeps_is_seen_normalised(env):
    run = _run(env)
    jobsdata.load_files([run], descriptions_cached=True)
    df, _ = jobsdata.load_files([run], descriptions_cached=True)
    assert df["is_seen"].tolist() == ["no", "no"]


def test_offline_source_reads_the_copy_and_keys_by_the_drive_path(env, monkeypatch):
    master = _master(env)
    jobsdata.load_files([master], descriptions_cached=True)
    for f in master.parent.iterdir():
        f.unlink()
    master.parent.rmdir()
    calls = _count_reads(monkeypatch)
    offline: list = []
    df, id_to_path = jobsdata.load_files([master], offline=offline,
                                         descriptions_cached=True)
    assert [p for p, _, _ in offline] == [master]
    assert _full_reads(calls) == []                    # the copy has the same stamp
    assert not set(DESC) & set(df.columns)
    assert set(df["_source"]) == {str(master)}
    assert id_to_path["1"] == master
    assert jobsdata.job_description("1", str(master))["job_summary"] == "master summary one"


def test_offline_source_never_cached_before_is_stored_from_the_copy(env):
    master = _master(env)
    jobsdata.load_files([master])                      # default mode: copy, no cache
    for f in master.parent.iterdir():
        f.unlink()
    master.parent.rmdir()
    df, _ = jobsdata.load_files([master], offline=[], descriptions_cached=True)
    assert not set(DESC) & set(df.columns)
    assert jobsdata.job_description("2", str(master)) == {"job_summary": "master summary two"}
    assert desc_cache.is_current(str(master), jobsdata.mirror_path(master))


def test_an_unusable_cache_loads_the_old_full_frame(env, monkeypatch):
    master, run = _master(env), _run(env)
    monkeypatch.setattr(desc_cache, "store", lambda *a, **k: False)
    problems: list = []
    offline: list = []
    df, _ = jobsdata.load_files([master, run, env / "broken.csv.gz"], problems=problems,
                                offline=offline, descriptions_cached=True)
    assert {"job_summary", "job_description_md"} <= set(df.columns)
    assert df.set_index("job_posting_id").loc["3", "job_description_md"] == "## run md three"
    assert problems == [] and offline == []            # reported once, not twice


def test_an_unusable_cache_after_a_warm_source_still_returns_full_text(env, monkeypatch):
    master, run = _master(env), _run(env)
    jobsdata.load_files([master], descriptions_cached=True)   # master warm
    monkeypatch.setattr(desc_cache, "store", lambda *a, **k: False)
    df, _ = jobsdata.load_files([master, run], descriptions_cached=True)
    by_id = df.set_index("job_posting_id")
    assert by_id.loc["2", "job_summary"] == "master summary two"
    assert by_id.loc["3", "job_description_md"] == "## run md three"


def test_an_unreadable_source_is_reported_once_in_cached_mode(env):
    bad = env / "repo" / "daily" / "bad_scored.csv.gz"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\x1f\x8b not really gzip")
    problems: list = []
    df, _ = jobsdata.load_files([_run(env), bad], problems=problems,
                                descriptions_cached=True)
    assert [p for p, _ in problems] == [bad]
    assert sorted(df["job_posting_id"]) == ["1", "3"]


def test_sources_no_longer_loaded_are_pruned(env):
    master, run = _master(env), _run(env)
    jobsdata.load_files([master, run], descriptions_cached=True)
    jobsdata.load_files([run], descriptions_cached=True)
    assert not desc_cache.is_current(str(master), master)
    assert desc_cache.is_current(str(run), run)


def test_job_description_is_empty_for_an_unknown_job(env):
    assert jobsdata.job_description("nope", "nowhere") == {}
