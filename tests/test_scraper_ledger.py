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


LEDGER_HEADER = ["job_posting_id", "collected_date", "job_title", "keywords"]


def _ledger(sandbox) -> dict:
    df = pd.read_csv(sandbox / "collected_ids_ledger.csv", dtype=str)
    assert list(df.columns) == LEDGER_HEADER
    return dict(zip(df["job_posting_id"], df["collected_date"]))


def _ledger_rows(sandbox) -> dict:
    """id -> (collected_date, job_title, keywords) as the file holds them."""
    df = pd.read_csv(sandbox / "collected_ids_ledger.csv", dtype=str,
                     keep_default_na=False)
    assert list(df.columns) == LEDGER_HEADER
    return {r.job_posting_id: (r.collected_date, r.job_title, r.keywords)
            for r in df.itertuples()}


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


def test_recording_prunes_rows_older_than_60_days(sandbox, monkeypatch):
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")   # window under 60 days
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
    # the default 90-day window is longer than LEDGER_KEEP_DAYS, so it sets the cutoff
    _run_csv(sandbox, "evening", _days_ago(95), [("ancient", _days_ago(95))])
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


# --- review fixes: mixed date formats, a saved seed, the prune floor, the cap ---

def test_mixed_date_formats_inside_one_source_all_parse():
    """pandas infers ONE format per column unless told the column is ISO 8601, so
    a plain date next to an offset or a space-separated time turned to NaT."""
    df = pd.DataFrame({"job_posting_id": ["a", "b", "c"],
                       "extracted_date": ["2026-10-05", "2026-10-05T12:00:00+00:00",
                                          "2026-10-05 08:00:00"]})
    dated = scraper._parse_dates(df)
    assert dated["_extracted"].notna().all()


def test_the_seed_reads_mixed_date_formats_inside_one_run_file(sandbox):
    """A mixed-format run file must not fall back to the file-name date for the
    rows pandas could not infer a format for."""
    label = scraper.RUN_LABELS[0]
    d = sandbox / label
    d.mkdir()
    (d / f"linkedin_jobs_{_days_ago(3)}_{label}.csv").write_text(
        "job_posting_id,extracted_date\n"
        f"plain,{_days_ago(2)}\n"
        f"offset,{_days_ago(1)}T12:00:00+00:00\n", encoding="utf-8")
    seeded = scraper.seed_ledger_from_run_files()
    assert dict(zip(seeded["job_posting_id"], seeded["collected_date"])) == {
        "plain": _days_ago(2), "offset": _days_ago(1)}


def test_a_rebuilt_ledger_is_saved_so_the_run_files_are_read_once(sandbox, monkeypatch):
    _run_csv(sandbox, "morning", _days_ago(2), [("job", _days_ago(2))])
    assert scraper.load_exclude_ids() == ["job"]
    assert _ledger(sandbox) == {"job": _days_ago(2)}

    def boom():
        raise AssertionError("the saved ledger should be read, not rebuilt again")

    monkeypatch.setattr(scraper, "seed_ledger_from_run_files", boom)
    assert scraper.load_exclude_ids() == ["job"]


def test_a_rebuilt_ledger_that_cannot_be_saved_warns_and_still_counts(
        sandbox, monkeypatch, capsys):
    _run_csv(sandbox, "morning", _days_ago(2), [("job", _days_ago(2))])

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    assert scraper.load_exclude_ids() == ["job"]
    assert not (sandbox / "collected_ids_ledger.csv").exists()
    out = capsys.readouterr().out
    assert "WARNING:" in out and "collected_ids_ledger.csv" in out


def test_the_ledger_keeps_every_day_the_skip_list_window_wants(sandbox, monkeypatch):
    """A 90-day window over a 60-day ledger would forget ids the window still keeps."""
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "90")
    _write_ledger(sandbox, [("day75", _days_ago(75)), ("day95", _days_ago(95))])
    scraper.record_collected_ids(["today"], _days_ago(0))
    assert _ledger(sandbox) == {"day75": _days_ago(75), "today": _days_ago(0)}


def test_the_ledger_still_keeps_60_days_under_a_short_window(sandbox, monkeypatch):
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "14")
    _write_ledger(sandbox, [("day59", _days_ago(59)), ("day61", _days_ago(61))])
    scraper.record_collected_ids(["today"], _days_ago(0))
    assert _ledger(sandbox) == {"day59": _days_ago(59), "today": _days_ago(0)}


def test_a_job_collected_yesterday_survives_a_cap_that_really_binds(sandbox, monkeypatch):
    """First found long ago, collected again yesterday, with a cap far smaller than
    the skip list: it must ride the ledger date all the way into the inputs."""
    monkeypatch.setenv(scraper.MAX_EXCLUDE_IDS_ENV, str(scraper.MIN_EXCLUDE_IDS))
    rows = [(f"j{n}", _days_ago(2 + n % 40)) for n in range(200)]
    rows.append(("target", _days_ago(80)))
    _write_master(sandbox / "linkedin_jobs_master.csv", rows)

    def sent():
        ids = scraper.load_exclude_ids()
        assert len(ids) == 201
        inputs = scraper.build_inputs(ids, max_keywords=1, limit_per_input=100)
        kept = inputs[0]["jobs_to_not_include"]
        assert len(kept) == scraper.MIN_EXCLUDE_IDS          # the cap bound
        return kept

    assert "target" not in sent()                             # the premise
    _write_ledger(sandbox, [("target", _days_ago(1))])
    assert "target" in sent()


# --- each job's title and the keyword search(es) that returned it ---------------
#
# The next step builds a skip list per search keyword (selective_search only
# returns jobs whose title holds the keyword), so the ledger remembers each id's
# title and which keyword searches found it.

def test_norm_text_lowercases_strips_quotes_and_collapses_punctuation():
    assert scraper._norm_text('"Machine Learning" Engineer') == "machine learning engineer"
    assert scraper._norm_text("  Sr. Software-Engineer (AI/ML)  ") == \
        "sr software engineer ai ml"
    assert scraper._norm_text("Women's Health") == "womens health"
    assert scraper._norm_text("\u201cQuant\u201d Dev\u2019s") == "quant devs"
    assert scraper._norm_text("C++ / C# Dev_Ops") == "c c dev ops"
    assert scraper._norm_text(None) == ""
    assert scraper._norm_text(float("nan")) == ""
    assert scraper._norm_text("---") == ""


def test_the_old_call_shape_still_records_ids_without_title_or_keyword(sandbox):
    scraper.record_collected_ids(["a"], _days_ago(0))
    assert _ledger_rows(sandbox) == {"a": (_days_ago(0), "", "")}


def test_a_two_column_ledger_is_read_and_upgraded_on_the_next_write(sandbox):
    _write_ledger(sandbox, [("old", _days_ago(3))])
    assert scraper.load_exclude_ids() == ["old"]
    frame = scraper._ledger_frame()
    assert frame.loc[0, "job_title"] == "" and frame.loc[0, "keywords"] == ""
    scraper.record_collected_ids(["new"], _days_ago(0),
                                 titles={"new": "Data Scientist"},
                                 keywords={"new": {"Data Scientist"}})
    assert _ledger_rows(sandbox) == {
        "old": (_days_ago(3), "", ""),
        "new": (_days_ago(0), "Data Scientist", "data scientist")}


def test_keywords_are_unioned_across_runs(sandbox):
    scraper.record_collected_ids(["a", "b"], _days_ago(2),
                                 keywords={"a": {"Software Engineer"}, "b": {"Quant"}})
    scraper.record_collected_ids(["a"], _days_ago(1),
                                 keywords={"a": {"Backend Engineer", "software engineer"}})
    # an OLDER run's keyword is still a keyword that found the job
    scraper.record_collected_ids(["b"], _days_ago(5), keywords={"b": {"Data Analyst"}})
    rows = _ledger_rows(sandbox)
    assert rows["a"] == (_days_ago(1), "", "backend engineer|software engineer")
    assert rows["b"] == (_days_ago(2), "", "data analyst|quant")


def test_the_newest_non_empty_title_wins(sandbox):
    scraper.record_collected_ids(["a", "b"], _days_ago(3),
                                 titles={"a": "ML Engineer", "b": "Old Title"})
    scraper.record_collected_ids(["a", "b"], _days_ago(1),
                                 titles={"a": "", "b": "New Title"})
    scraper.record_collected_ids(["b"], _days_ago(6), titles={"b": "Ancient Title"})
    rows = _ledger_rows(sandbox)
    assert rows["a"] == (_days_ago(1), "ML Engineer", "")
    assert rows["b"] == (_days_ago(1), "New Title", "")


def test_bad_title_or_keyword_values_never_crash_the_recording(sandbox, capsys):
    scraper.record_collected_ids(["a", "b"], _days_ago(0),
                                 titles={"a": None, "b": float("nan")},
                                 keywords={"a": None, "b": "Quant"})
    assert _ledger_rows(sandbox) == {"a": (_days_ago(0), "", ""),
                                     "b": (_days_ago(0), "", "quant")}
    assert "WARNING:" not in capsys.readouterr().out


def test_the_ledger_frame_exposes_titles_and_keywords_in_skip_list_order(sandbox):
    scraper.record_collected_ids(["new"], _days_ago(1), titles={"new": "B"},
                                 keywords={"new": {"k2"}})
    scraper.record_collected_ids(["old"], _days_ago(4), titles={"old": "A"},
                                 keywords={"old": {"k1"}})
    frame = scraper._ledger_frame()
    assert frame.columns.tolist() == ["job_posting_id", "extracted_date",
                                      "job_title", "keywords"]
    assert frame["job_posting_id"].tolist() == ["old", "new"]
    assert frame["job_title"].tolist() == ["A", "B"]
    assert frame["keywords"].tolist() == ["k1", "k2"]
    assert scraper.load_exclude_ids() == ["old", "new"]


def test_the_seed_carries_titles(sandbox):
    label = scraper.RUN_LABELS[0]
    d = sandbox / label
    d.mkdir()
    (d / f"linkedin_jobs_{_days_ago(3)}_{label}.csv").write_text(
        "job_posting_id,job_title,extracted_date\n"
        f"a,Quant Researcher,{_days_ago(3)}\n"
        f"b,,{_days_ago(3)}\n", encoding="utf-8")
    (d / f"linkedin_jobs_{_days_ago(1)}_{label}.csv").write_text(
        "job_posting_id,job_title,extracted_date\n"
        f"a,,{_days_ago(1)}\n", encoding="utf-8")
    seeded = scraper.seed_ledger_from_run_files()
    got = {r.job_posting_id: (r.collected_date, r.job_title, r.keywords)
           for r in seeded.itertuples()}
    assert got == {"a": (_days_ago(1), "Quant Researcher", ""),
                   "b": (_days_ago(3), "", "")}
    scraper.record_collected_ids(["c"], _days_ago(0))         # persists the seed
    assert _ledger_rows(sandbox)["a"] == (_days_ago(1), "Quant Researcher", "")


def _save(sandbox, monkeypatch, rows):
    monkeypatch.setattr(scraper, "load_blocklist", lambda: [])
    scraper._save_results(rows, scraper.RUN_LABELS[0])
    return _ledger_rows(sandbox)


def test_a_run_records_the_title_and_the_flattened_discovery_keyword(sandbox, monkeypatch):
    """Bright Data echoes each search's input in discovery_input; json_normalize
    flattens it to discovery_input.keyword, and drop_unneeded_columns drops that
    before the run file is written, so it is read from the raw rows."""
    rows = _save(sandbox, monkeypatch, [
        {"job_posting_id": "1", "job_title": "Quant Developer",
         "discovery_input": {"keyword": '"Quant"', "location": "United States",
                             "jobs_to_not_include": ["x"]}},
        {"job_posting_id": "2", "job_title": "Gen AI Engineer",
         "discovery_input": {"keyword": "GenAI Engineer"}},
    ])
    today = _days_ago(0)
    assert rows == {"1": (today, "Quant Developer", "quant"),
                    "2": (today, "Gen AI Engineer", "genai engineer")}
    saved = next((sandbox / scraper.RUN_LABELS[0]).glob("linkedin_jobs_*.csv"))
    assert not any(c.startswith("discovery_input")
                   for c in pd.read_csv(saved, nrows=0).columns)


def test_a_run_reads_a_discovery_input_kept_as_a_string_or_dict(sandbox, monkeypatch):
    """Some snapshots carry discovery_input as one column: a Python repr or JSON
    string (a CSV-shaped download), or a dict json_normalize did not flatten."""
    rows = [
        {"job_posting_id": "1", "job_title": "A",
         "discovery_input": "{'keyword': 'Data Scientist', 'remote': 'Remote'}"},
        {"job_posting_id": "2", "job_title": "B",
         "discovery_input": '{"keyword": "Machine Learning"}'},
        {"job_posting_id": "3", "job_title": "C", "discovery_input": "not a dict"},
        {"job_posting_id": "4", "job_title": "D", "input.keyword": "Backend Engineer"},
    ]
    today = _days_ago(0)
    assert _save(sandbox, monkeypatch, rows) == {
        "1": (today, "A", "data scientist"),
        "2": (today, "B", "machine learning"),
        "3": (today, "C", ""),
        "4": (today, "D", "backend engineer")}
    df = pd.DataFrame({"job_posting_id": ["5", "6"],
                       "discovery_input": [{"keyword": "Quant Analyst"}, None]})
    assert scraper._keywords_by_id(df) == {"5": {"quant analyst"}}


def test_one_job_found_by_several_searches_in_one_run_keeps_every_keyword(
        sandbox, monkeypatch):
    """The rows are deduped by id before the run file is written, so the keywords
    come from every raw row of the id, not just the one that is kept."""
    rows = _save(sandbox, monkeypatch, [
        {"job_posting_id": "1", "job_title": "",
         "discovery_input": {"keyword": "Software Engineer"}},
        {"job_posting_id": "1", "job_title": "ML Software Engineer",
         "discovery_input": {"keyword": "Machine Learning Engineer"}},
        {"job_posting_id": "1", "job_title": "ML Software Engineer",
         "discovery_input": {"keyword": "software engineer"}},
        {"job_posting_id": "2", "job_title": "Analyst"},
    ])
    today = _days_ago(0)
    assert rows == {
        "1": (today, "ML Software Engineer", "machine learning engineer|software engineer"),
        "2": (today, "Analyst", "")}
