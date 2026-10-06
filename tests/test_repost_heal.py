"""Repost score reuse must carry all six score columns, and reused rows that an
earlier build wrote blank are healed from their source row.

`reuse_repost_scores` copies `_REPOST_REUSE_COLS` (score, reason, deep_score,
strengths, gaps, recommendation) from a master row, so `load_master_for_reuse`
must project all six: a loader that projects only the identity columns plus
`score` gives a reused repost a real score and a blank reason, deep_score,
strengths, gaps and recommendation. The first section checks the loader; the
second heals the rows already written blank.

Every test points the master and the run folders at tmp_path.
"""
import asyncio
import gzip
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import score_jobs as sj  # noqa: E402

TODAY = date(2026, 9, 28)
FIVE = ("reason", "deep_score", "strengths", "gaps", "recommendation")


def _master_record(job_id, **overrides):
    row = {
        "job_posting_id": job_id,
        "job_title": "Data Engineer",
        "company_name": "Acme",
        "job_location": "Seattle, WA",
        "job_description_md": "Build data pipelines end to end.",
        "extracted_date": "2026-09-20",
        "score": 5,
        "reason": "good fit",
        "deep_score": 8,
        "strengths": "python | sql",
        "gaps": "no spark",
        "recommendation": "apply",
    }
    row.update(overrides)
    return row


def _incoming(job_id, **overrides):
    row = {
        "job_posting_id": job_id,
        "job_title": "Data Engineer",
        "company_name": "Acme",
        "job_location": "Seattle, WA",
        "job_description_md": "Build data pipelines end to end.",
        "filtered_out": False,
    }
    row.update(overrides)
    return row


def _write_master(tmp_path, monkeypatch, records):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame(records).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    return master


# --- the loader reads all six reuse columns --------------------------------------

def test_load_master_for_reuse_reads_the_five_text_and_deep_columns(tmp_path, monkeypatch):
    _write_master(tmp_path, monkeypatch, [_master_record("OLD-1")])

    out = sj.load_master_for_reuse()

    assert out is not None
    for col in ("score",) + FIVE:
        assert col in out.columns
    row = out.iloc[0]
    assert row["reason"] == "good fit"
    assert row["deep_score"] == 8
    assert row["strengths"] == "python | sql"
    assert row["gaps"] == "no spark"
    assert row["recommendation"] == "apply"


def test_a_reused_repost_gets_all_six_values_from_the_loaded_master(tmp_path, monkeypatch):
    _write_master(tmp_path, monkeypatch, [_master_record("OLD-1")])
    master = sj.load_master_for_reuse()
    df = pd.DataFrame([_incoming("NEW-1")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=TODAY)

    assert n == 1
    row = out.iloc[0]
    assert row["score"] == 5
    assert row["deep_score"] == 8
    assert row["reason"] == "good fit"
    assert row["strengths"] == "python | sql"
    assert row["gaps"] == "no spark"
    assert row["recommendation"] == "apply"
    assert row["score_reused_from"] == "OLD-1"
    assert bool(row["score_reused"]) is True


def test_reposts_of_sources_with_and_without_gaps_both_reuse(tmp_path, monkeypatch):
    """A source with blank gaps loads NaN; a second source with gap text must
    still copy into the same column (pandas 3 refuses str into a float column)."""
    _write_master(tmp_path, monkeypatch, [
        _master_record("OLD-A", gaps="", job_description_md="Job A description text."),
        _master_record("OLD-B", gaps="no spark", job_title="Data Analyst",
                       job_description_md="Job B description text."),
    ])
    master = sj.load_master_for_reuse()
    df = pd.DataFrame([
        _incoming("NEW-A", job_description_md="Job A description text."),
        _incoming("NEW-B", job_title="Data Analyst",
                  job_description_md="Job B description text."),
    ])

    out, n = sj.reuse_repost_scores(df, master, 30, today=TODAY)

    assert n == 2
    by_id = out.set_index("job_posting_id")
    assert by_id.loc["NEW-A", "score_reused_from"] == "OLD-A"
    assert by_id.loc["NEW-B", "score_reused_from"] == "OLD-B"
    assert by_id.loc["NEW-B", "gaps"] == "no spark"
    assert by_id.loc["NEW-B", "reason"] == "good fit"
    assert by_id.loc["NEW-B", "deep_score"] == 8


def test_an_older_master_without_deep_score_still_loads_and_reuses_the_score(
        tmp_path, monkeypatch):
    record = _master_record("OLD-1")
    for col in ("deep_score", "strengths", "gaps", "recommendation"):
        del record[col]
    _write_master(tmp_path, monkeypatch, [record])

    master = sj.load_master_for_reuse()
    assert master is not None
    assert "deep_score" not in master.columns
    assert master.iloc[0]["reason"] == "good fit"

    out, n = sj.reuse_repost_scores(pd.DataFrame([_incoming("NEW-1")]), master, 30, today=TODAY)

    assert n == 1
    assert out.iloc[0]["score"] == 5
    assert out.iloc[0]["score_reused_from"] == "OLD-1"


def test_a_reused_repost_keeps_all_six_values_through_run_scoring(tmp_path, monkeypatch):
    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"a reused row must not reach the pool ({name})")

    _write_master(tmp_path, monkeypatch, [_master_record("OLD-1", gaps="")])
    master = sj.load_master_for_reuse()
    df, n = sj.reuse_repost_scores(pd.DataFrame([_incoming("NEW-1")]), master, 30, today=TODAY)
    assert n == 1

    merged = asyncio.run(sj.run_scoring(NoCalls(), "resume", df))

    row = merged.iloc[0]
    assert row["score"] == 5
    assert row["deep_score"] == 8
    assert row["reason"] == "good fit"
    assert row["strengths"] == "python | sql"
    assert pd.isna(row["gaps"]) or row["gaps"] == ""
    assert row["recommendation"] == "apply"


# --- heal the reused rows already written blank ----------------------------------

NAN = float("nan")
SECRET = "distinctive reason text 9f3a"   # must never reach the one-shot's output


def _src(job_id="OLD-1", **overrides):
    row = {"job_posting_id": job_id, "reason": "good fit", "deep_score": "8.0",
           "strengths": "python | sql", "gaps": "no spark", "recommendation": "apply"}
    row.update(overrides)
    return row


def _reused(job_id="NEW-1", origin="OLD-1", **overrides):
    row = {"job_posting_id": job_id, "score_reused": True, "score_reused_from": origin,
           "reason": NAN, "deep_score": NAN, "strengths": NAN, "gaps": NAN,
           "recommendation": NAN}
    row.update(overrides)
    return row


def _apply(frame, healed):
    """What a caller does with the healed rows: fill the frame's blank cells."""
    out = frame.copy().astype(object).set_index("job_posting_id")
    out.update(healed.astype(object).set_index("job_posting_id"))
    return out.reset_index()


def test_heal_reused_rows_fills_every_blank_spelling():
    frame = pd.DataFrame([_reused(reason=None, deep_score="", strengths="nan", gaps="  ")])
    healed = sj.heal_reused_rows(frame, pd.DataFrame([_src()]))

    assert list(healed.columns) == ["job_posting_id", *FIVE]
    assert len(healed) == 1
    row = healed.iloc[0]
    assert row["job_posting_id"] == "NEW-1"
    assert row["reason"] == "good fit"
    assert row["deep_score"] == "8.0"
    assert row["strengths"] == "python | sql"
    assert row["gaps"] == "no spark"
    assert row["recommendation"] == "apply"


@pytest.mark.parametrize("flag", ["True", "true", "1", "1.0", True])
def test_heal_reused_rows_reads_every_truthy_spelling_of_score_reused(flag):
    frame = pd.DataFrame([_reused(score_reused=flag)])
    assert len(sj.heal_reused_rows(frame, pd.DataFrame([_src()]))) == 1


@pytest.mark.parametrize("flag", ["False", "false", "0", False, NAN, ""])
def test_heal_reused_rows_leaves_a_row_that_is_not_reused_alone(flag):
    frame = pd.DataFrame([_reused(score_reused=flag)])
    assert sj.heal_reused_rows(frame, pd.DataFrame([_src()])).empty


def test_heal_reused_rows_never_overwrites_a_non_blank_cell():
    frame = pd.DataFrame([_reused(reason="kept reason", gaps="kept gaps")])
    healed = sj.heal_reused_rows(frame, pd.DataFrame([_src()]))

    row = healed.iloc[0]
    assert pd.isna(row["reason"]) and pd.isna(row["gaps"])      # left for update() to skip
    assert row["deep_score"] == "8.0" and row["strengths"] == "python | sql"
    assert row["recommendation"] == "apply"


def test_heal_reused_rows_takes_nothing_from_a_blank_source_cell():
    frame = pd.DataFrame([_reused()])
    healed = sj.heal_reused_rows(frame, pd.DataFrame([_src(gaps="", strengths=None)]))

    row = healed.iloc[0]
    assert pd.isna(row["gaps"]) and pd.isna(row["strengths"])
    assert row["reason"] == "good fit"
    all_blank = pd.DataFrame([_src(reason="", deep_score=NAN, strengths="nan", gaps=None,
                                   recommendation=" ")])
    assert sj.heal_reused_rows(frame, all_blank).empty


def test_heal_reused_rows_skips_an_unknown_source_id():
    frame = pd.DataFrame([_reused(origin="GONE-9"), _reused("NEW-2", origin=NAN)])
    assert sj.heal_reused_rows(frame, pd.DataFrame([_src()])).empty


def test_heal_reused_rows_compares_ids_as_stripped_strings():
    frame = pd.DataFrame([_reused(origin=" OLD-1 ")])
    healed = sj.heal_reused_rows(frame, pd.DataFrame([_src(" OLD-1")]))
    assert len(healed) == 1


def test_heal_reused_rows_matches_an_id_a_float_column_turned_into_dot_zero():
    healed = sj.heal_reused_rows(pd.DataFrame([_reused(origin="123.0")]),
                                 pd.DataFrame([_src("123")]))
    assert healed["job_posting_id"].tolist() == ["NEW-1"]
    assert healed.iloc[0]["reason"] == "good fit"

    healed = sj.heal_reused_rows(pd.DataFrame([_reused(origin="123")]),
                                 pd.DataFrame([_src("123.0")]))
    assert healed["job_posting_id"].tolist() == ["NEW-1"]

    # only a bare ".0" tail is float noise; other ids stay distinct
    assert sj.heal_reused_rows(pd.DataFrame([_reused(origin="123.5")]),
                               pd.DataFrame([_src("123")])).empty


def test_heal_reused_rows_returns_only_the_changed_rows_and_is_idempotent():
    frame = pd.DataFrame([
        _reused("NEW-1"),
        _reused("NEW-2", reason="own", deep_score="7.0", strengths="own", gaps="own",
                recommendation="own"),          # already complete
        {"job_posting_id": "PLAIN-1", "score_reused": False, "score_reused_from": NAN,
         "reason": NAN, "deep_score": NAN, "strengths": NAN, "gaps": NAN,
         "recommendation": NAN},
    ])
    sources = pd.DataFrame([_src()])

    healed = sj.heal_reused_rows(frame, sources)
    assert healed["job_posting_id"].tolist() == ["NEW-1"]

    again = sj.heal_reused_rows(_apply(frame, healed), sources)
    assert again.empty
    assert list(again.columns) == ["job_posting_id", *FIVE]


def test_heal_reused_rows_tolerates_frames_that_lack_the_reuse_columns():
    sources = pd.DataFrame([_src()])
    assert sj.heal_reused_rows(pd.DataFrame(), sources).empty
    assert sj.heal_reused_rows(pd.DataFrame([{"job_posting_id": "N"}]), sources).empty
    assert sj.heal_reused_rows(pd.DataFrame([_reused()]), pd.DataFrame()).empty


# The master fixture below is written through pandas (the writer update_master_scores
# uses), so its line endings match the rewrite and a text diff is a byte diff.

MASTER_COLS = ["job_posting_id", "job_title", "external_ref", "score", "reason",
               "deep_score", "strengths", "gaps", "recommendation", "score_reused",
               "score_reused_from"]


def _m(job_id, **kw):
    row = dict.fromkeys(MASTER_COLS, "")
    row.update(job_posting_id=job_id, job_title="Data Engineer", external_ref="007",
               score="5.0")
    row.update(kw)
    return row


def _master_rows():
    return [
        _m("SRC-1", reason=SECRET + ", with a comma", deep_score="8.0",
           strengths="python | sql", gaps="no spark", recommendation="apply",
           score_reused="False"),
        _m("NEW-1", score_reused="True", score_reused_from="SRC-1"),      # all five blank
        _m("NEW-2", reason="kept text", score_reused="True",
           score_reused_from="SRC-1"),                                     # partly filled
        _m("PLAIN-1", score="", score_reused="False"),                     # no score, no reuse flag
        _m("NEW-3", score_reused="True", score_reused_from="GONE-9"),      # unknown source
        _m("SRC-2", reason="second", deep_score="6.5", strengths="sql", gaps="",
           recommendation="skip", score_reused="False"),
        _m("NEW-4", score_reused="true", score_reused_from="SRC-2"),       # source gaps blank
    ]


def _write_master_rows(tmp_path, monkeypatch, rows):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame(rows, dtype=object).to_csv(master, index=False, encoding="utf-8")
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    return master


def _read_all(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False).set_index("job_posting_id")


def test_heal_master_reuse_fills_each_blank_reused_row_and_leaves_the_rest_byte_stable(
        tmp_path, monkeypatch):
    master = _write_master_rows(tmp_path, monkeypatch, _master_rows())
    before = master.read_text(encoding="utf-8").splitlines()
    before_frame = _read_all(master)

    healed = sj.heal_master_reuse()

    assert healed == 3                          # NEW-1, NEW-2 (four cells) and NEW-4
    after = _read_all(master)
    n1 = after.loc["NEW-1"]
    assert n1["reason"] == SECRET + ", with a comma"
    assert (n1["deep_score"], n1["strengths"], n1["gaps"], n1["recommendation"]) == (
        "8.0", "python | sql", "no spark", "apply")
    n2 = after.loc["NEW-2"]
    assert n2["reason"] == "kept text"          # a non-blank cell is never overwritten
    assert n2["deep_score"] == "8.0" and n2["gaps"] == "no spark"
    n4 = after.loc["NEW-4"]
    assert n4["reason"] == "second" and n4["deep_score"] == "6.5"
    assert n4["gaps"] == ""                     # the source's gaps is blank too

    # Every other row is untouched, line for line, and no column changed shape.
    after_lines = master.read_text(encoding="utf-8").splitlines()
    assert after_lines[0] == before[0]
    assert len(after_lines) == len(before)
    for job_id in ("SRC-1", "PLAIN-1", "NEW-3", "SRC-2"):
        i = list(before_frame.index).index(job_id) + 1
        assert after_lines[i] == before[i]
    assert list(after.columns) == list(before_frame.columns)
    assert after.loc["SRC-1", "external_ref"] == "007"
    assert after.loc["SRC-1", "score"] == "5.0"


def test_heal_master_reuse_is_idempotent(tmp_path, monkeypatch):
    master = _write_master_rows(tmp_path, monkeypatch, _master_rows())
    assert sj.heal_master_reuse() == 3
    once = master.read_bytes()

    assert sj.heal_master_reuse() == 0
    assert master.read_bytes() == once


def test_heal_master_reuse_dry_run_counts_and_writes_nothing(tmp_path, monkeypatch):
    master = _write_master_rows(tmp_path, monkeypatch, _master_rows())
    before = master.read_bytes()

    assert sj.heal_master_reuse(dry_run=True) == 3
    assert master.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [master.name]   # no temp file left


def test_heal_master_reuse_is_zero_without_a_master_or_the_origin_column(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    assert sj.heal_master_reuse() == 0                                    # no master

    rows = [{k: v for k, v in r.items() if k != "score_reused_from"} for r in _master_rows()]
    master = _write_master_rows(tmp_path, monkeypatch, rows)
    before = master.read_bytes()
    assert sj.heal_master_reuse() == 0                                    # older schema
    assert master.read_bytes() == before


def _run_file_rows():
    return [
        {"job_posting_id": "NEW-1", "job_title": "Data Engineer", "score": "5.0",
         "reason": "", "deep_score": "", "strengths": "", "gaps": "", "recommendation": "",
         "score_reused": "True", "score_reused_from": "SRC-1", "is_seen": "no"},
        {"job_posting_id": "FRESH-1", "job_title": "Data Analyst", "score": "3.0",
         "reason": "scored fresh", "deep_score": "", "strengths": "", "gaps": "",
         "recommendation": "", "score_reused": "False", "score_reused_from": "",
         "is_seen": "yes"},
    ]


def _write_run_gz(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, dtype=object).to_csv(path, index=False, encoding="utf-8",
                                            compression="gzip")


def _read_run(path):
    return pd.read_csv(path, dtype=object, keep_default_na=False).set_index("job_posting_id")


def test_heal_run_files_heals_only_the_blank_reused_row_of_a_gz_file(tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    run = tmp_path / "morning" / "linkedin_jobs_2026-09-25_morning_scored.csv.gz"
    _write_run_gz(run, _run_file_rows())
    before = _read_run(run)

    assert sj.heal_run_files() == (1, 1)

    after = _read_run(run)
    n1 = after.loc["NEW-1"]
    assert n1["reason"] == SECRET + ", with a comma"
    assert (n1["deep_score"], n1["strengths"], n1["gaps"], n1["recommendation"]) == (
        "8.0", "python | sql", "no spark", "apply")
    assert n1["score"] == "5.0" and n1["is_seen"] == "no"
    pd.testing.assert_series_equal(after.loc["FRESH-1"], before.loc["FRESH-1"])
    assert list(after.columns) == list(before.columns)
    with gzip.open(run, "rb") as fh:                       # still a gzip file
        assert fh.read(4)
    assert sorted(p.name for p in run.parent.iterdir()) == [run.name]   # no temp file left


def test_heal_run_files_dry_run_reports_and_writes_nothing(tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    run = tmp_path / "evening" / "linkedin_jobs_2026-09-25_evening_scored.csv.gz"
    _write_run_gz(run, _run_file_rows())
    before = run.read_bytes()

    assert sj.heal_run_files(dry_run=True) == (1, 1)
    assert run.read_bytes() == before


def test_heal_run_files_is_idempotent_and_skips_files_with_nothing_to_heal(
        tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    healable = tmp_path / "morning" / "linkedin_jobs_a_scored.csv.gz"
    _write_run_gz(healable, _run_file_rows())
    clean = tmp_path / "night" / "linkedin_jobs_b_scored.csv.gz"
    _write_run_gz(clean, _run_file_rows()[1:])
    plain = tmp_path / "afternoon" / "linkedin_jobs_c_scored.csv"
    plain.parent.mkdir()
    pd.DataFrame(_run_file_rows(), dtype=object).to_csv(plain, index=False, encoding="utf-8")
    clean_before = clean.read_bytes()

    assert sj.heal_run_files() == (2, 2)                   # the gz file and the plain csv
    assert clean.read_bytes() == clean_before
    assert _read_run(plain).loc["NEW-1", "reason"].startswith(SECRET)
    assert not plain.read_bytes().startswith(b"\x1f\x8b")  # a plain file stays plain

    assert sj.heal_run_files() == (0, 0)


def test_heal_run_files_ignores_temp_files_other_folders_and_input_csvs(
        tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    stray = tmp_path / "morning" / "linkedin_jobs_a_scored.csv.k3j9x1.tmp"
    stray.parent.mkdir()
    pd.DataFrame(_run_file_rows(), dtype=object).to_csv(stray, index=False)
    raw_input = tmp_path / "morning" / "linkedin_jobs_a.csv"
    pd.DataFrame(_run_file_rows(), dtype=object).to_csv(raw_input, index=False)
    elsewhere = tmp_path / "somewhere_else" / "linkedin_jobs_z_scored.csv.gz"
    _write_run_gz(elsewhere, _run_file_rows())
    snapshot = {p: p.read_bytes() for p in (stray, raw_input, elsewhere)}

    assert sj.heal_run_files() == (0, 0)
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_heal_run_files_is_zero_without_a_master(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    run = tmp_path / "morning" / "linkedin_jobs_a_scored.csv.gz"
    _write_run_gz(run, _run_file_rows())
    before = run.read_bytes()

    assert sj.heal_run_files() == (0, 0)
    assert run.read_bytes() == before


def test_heal_run_files_skips_an_unreadable_file_and_still_heals_the_others(
        tmp_path, monkeypatch, capsys):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    bad = tmp_path / "morning" / "linkedin_jobs_bad_scored.csv.gz"
    bad.parent.mkdir()
    bad.write_bytes(b"not a gzip file at all")
    good = tmp_path / "evening" / "linkedin_jobs_good_scored.csv.gz"
    _write_run_gz(good, _run_file_rows())

    assert sj.heal_run_files() == (1, 1)
    assert bad.read_bytes() == b"not a gzip file at all"
    assert "linkedin_jobs_bad_scored.csv.gz" in capsys.readouterr().out


def test_heal_run_files_skips_a_file_it_cannot_write_and_still_heals_the_other(
        tmp_path, monkeypatch, capsys):
    """A PermissionError from the atomic replace (Drive or the dashboard holding the
    file on Windows) skips that one file: a line names it and it adds nothing to the counts."""
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    locked = tmp_path / "morning" / "linkedin_jobs_locked_scored.csv.gz"
    free = tmp_path / "evening" / "linkedin_jobs_free_scored.csv.gz"
    _write_run_gz(locked, _run_file_rows())
    _write_run_gz(free, _run_file_rows())
    locked_before = locked.read_bytes()
    real_write = sj._atomic_to_csv

    def write(df, path, **kwargs):
        if Path(path) == locked:
            raise PermissionError(13, "file is in use")
        real_write(df, path, **kwargs)

    monkeypatch.setattr(sj, "_atomic_to_csv", write)

    assert sj.heal_run_files() == (1, 1)                   # only the free file counts

    assert locked.read_bytes() == locked_before
    assert _read_run(free).loc["NEW-1", "reason"].startswith(SECRET)
    out = capsys.readouterr().out
    assert str(locked) in out and "PermissionError" in out
    assert SECRET not in out                                # the line carries no row content


def test_heal_run_files_reads_no_master_when_there_are_no_run_files(tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())

    def no_read():
        raise AssertionError("the master must not be read with no run files")

    monkeypatch.setattr(sj, "_read_master_for_heal", no_read)
    assert sj.heal_run_files() == (0, 0)
    assert sj.heal_run_files(dry_run=True) == (0, 0)


# --- main(): the per-run heal and the one-shot ----------------------------------

def _main_args(**kw):
    ns = {"csv": None, "heal_reused": False, "dry_run": False}
    ns.update(kw)
    return SimpleNamespace(**ns)


def _stub_run(monkeypatch, **args):
    """Everything main() touches before the heal step, faked; returns the log."""
    seen = {"rescored": 0, "stats": None}

    class Pool:
        def stats(self):
            return {}

    async def rescore(pool, resume, *, jev_run=None):
        seen["rescored"] += 1
        return 0, 0

    monkeypatch.setattr(sj, "parse_args", lambda: _main_args(**args))
    monkeypatch.setattr(sj, "load_resume", lambda: "resume")
    monkeypatch.setattr(sj, "latest_input_csv", lambda: None)
    monkeypatch.setattr(sj, "make_jev_judge", lambda: None)
    monkeypatch.setattr(sj, "make_pool", lambda required=True: Pool())
    monkeypatch.setattr(sj, "rescore_master_failures", rescore)
    monkeypatch.setattr(sj, "append_run_stats", lambda stats: seen.update(stats=stats))
    return seen


def test_main_prints_the_healed_count_only_when_it_is_positive(monkeypatch, capsys):
    seen = _stub_run(monkeypatch)
    monkeypatch.setattr(sj, "heal_master_reuse", lambda dry_run=False: 3)
    asyncio.run(sj.main())
    assert "Healed 3 reused rows in the master" in capsys.readouterr().out

    monkeypatch.setattr(sj, "heal_master_reuse", lambda dry_run=False: 0)
    asyncio.run(sj.main())
    assert "Healed" not in capsys.readouterr().out
    assert seen["rescored"] == 2


@pytest.mark.parametrize("error", [OSError("disk on fire"), ValueError("bad csv"),
                                   TypeError("unexpected cell type")])
def test_a_failing_heal_step_does_not_stop_the_run(monkeypatch, capsys, error):
    seen = _stub_run(monkeypatch)

    def boom(dry_run=False):
        raise error

    monkeypatch.setattr(sj, "heal_master_reuse", boom)

    asyncio.run(sj.main())

    out = capsys.readouterr().out
    assert "could not heal" in out.lower() and str(error) in out
    assert type(error).__name__ in out
    assert seen["rescored"] == 1            # the rescore pass still ran
    assert seen["stats"] is not None         # and the run's stats row was written


def test_the_heal_step_runs_after_this_runs_scores_reach_the_master(monkeypatch, tmp_path):
    """With a scored input, the heal comes after save_output's master update."""
    order = []
    seen = _stub_run(monkeypatch)
    inp = tmp_path / "morning" / "linkedin_jobs_x.csv"
    inp.parent.mkdir()
    pd.DataFrame([{"job_posting_id": "1", "job_title": "t", "company_name": "c",
                   "job_description_formatted": "<p>" + "text " * 30 + "</p>"}]
                 ).to_csv(inp, index=False)
    monkeypatch.setattr(sj, "latest_input_csv", lambda: inp)

    async def fake_scoring(pool, resume, df, *, jev_run=None):
        out = df.copy()
        for col, val in (("score", 4), ("reason", "r"), ("deep_score", None),
                         ("strengths", ""), ("gaps", ""), ("recommendation", "")):
            out[col] = val
        return out

    monkeypatch.setattr(sj, "run_scoring", fake_scoring)
    monkeypatch.setattr(sj, "load_master_for_reuse", lambda: None)
    monkeypatch.setattr(sj, "save_output",
                        lambda df, path: order.append("save_output") or path)
    monkeypatch.setattr(sj, "heal_master_reuse",
                        lambda dry_run=False: order.append("heal") or 0)

    asyncio.run(sj.main())

    assert order == ["save_output", "heal"]
    assert seen["rescored"] == 1


def test_heal_reused_flag_parses(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["score_jobs.py", "--heal-reused"])
    args = sj.parse_args()
    assert args.heal_reused is True and args.dry_run is False and args.csv is None

    monkeypatch.setattr(sys, "argv", ["score_jobs.py", "--heal-reused", "--dry-run"])
    args = sj.parse_args()
    assert args.heal_reused is True and args.dry_run is True

    monkeypatch.setattr(sys, "argv", ["score_jobs.py"])
    args = sj.parse_args()
    assert args.heal_reused is False and args.dry_run is False


def test_dry_run_without_heal_reused_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["score_jobs.py", "--dry-run"])
    with pytest.raises(SystemExit):
        sj.parse_args()
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--heal-reused" in err and "unrecognized" not in err


def _stub_one_shot(monkeypatch, **args):
    calls = []

    def refuse(name):
        def fail(*a, **k):
            raise AssertionError(f"--heal-reused must not reach {name}")
        return fail

    monkeypatch.setattr(sj, "parse_args", lambda: _main_args(heal_reused=True, **args))
    for name in ("load_resume", "make_jev_judge", "make_pool", "latest_input_csv",
                 "rescore_master_failures", "append_run_stats", "run_scoring"):
        monkeypatch.setattr(sj, name, refuse(name))
    monkeypatch.setattr(sj, "heal_run_files",
                        lambda dry_run=False: calls.append(("files", dry_run)) or (2, 5))
    monkeypatch.setattr(sj, "heal_master_reuse",
                        lambda dry_run=False: calls.append(("master", dry_run)) or 7)
    return calls


def test_heal_reused_runs_the_run_files_first_then_the_master_and_scores_nothing(
        monkeypatch, capsys):
    calls = _stub_one_shot(monkeypatch)

    asyncio.run(sj.main())

    assert calls == [("files", False), ("master", False)]
    out = capsys.readouterr().out
    assert "Healed 5 reused rows in 2 run files" in out
    assert "Healed 7 reused rows in the master" in out


def test_heal_reused_dry_run_passes_the_flag_and_says_would(monkeypatch, capsys):
    calls = _stub_one_shot(monkeypatch, dry_run=True)

    asyncio.run(sj.main())

    assert calls == [("files", True), ("master", True)]
    out = capsys.readouterr().out
    assert "Would heal 5 reused rows in 2 run files" in out
    assert "Would heal 7 reused rows in the master" in out


def test_heal_reused_prints_counts_only_never_row_contents(tmp_path, monkeypatch, capsys):
    _write_master_rows(tmp_path, monkeypatch, _master_rows())
    run = tmp_path / "morning" / "linkedin_jobs_a_scored.csv.gz"
    _write_run_gz(run, _run_file_rows())
    monkeypatch.setattr(sj, "parse_args", lambda: _main_args(heal_reused=True))
    monkeypatch.setattr(sj, "load_resume", lambda: pytest.fail("must not load the resume"))
    monkeypatch.setattr(sj, "make_pool", lambda **k: pytest.fail("must not build a pool"))

    asyncio.run(sj.main())

    out = capsys.readouterr().out
    assert SECRET not in out and "Data Engineer" not in out and "SRC-1" not in out
    assert _read_run(run).loc["NEW-1", "reason"].startswith(SECRET)
    assert _read_all(tmp_path / "linkedin_jobs_master.csv").loc["NEW-1", "reason"].startswith(
        SECRET)


UNREADABLE_MASTERS = [
    b"job_posting_id,score_reused,score_reused_from,reason\n1,True,2,\xff\xfe\x00\n",
    b"",
]


@pytest.mark.parametrize("content", UNREADABLE_MASTERS)
def test_an_unreadable_master_raises_oserror_naming_the_file(tmp_path, monkeypatch, content):
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_bytes(content)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    _write_run_gz(tmp_path / "morning" / "linkedin_jobs_a_scored.csv.gz", _run_file_rows())

    with pytest.raises(OSError, match="linkedin_jobs_master.csv"):
        sj.heal_master_reuse()
    with pytest.raises(OSError, match="linkedin_jobs_master.csv"):     # a run file exists
        sj.heal_run_files()
    assert master.read_bytes() == content


@pytest.mark.parametrize("content", UNREADABLE_MASTERS)
def test_heal_reused_reports_an_unreadable_master_and_exits_nonzero(
        tmp_path, monkeypatch, content):
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_bytes(content)
    monkeypatch.setattr(sj, "MASTER_CSV", master)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(sj, "parse_args", lambda: _main_args(heal_reused=True))
    monkeypatch.setattr(sj, "load_resume", lambda: pytest.fail("must not load the resume"))

    with pytest.raises(SystemExit) as exc:
        asyncio.run(sj.main())

    assert exc.value.code not in (0, None)
    assert "master" in str(exc.value.code).lower()


# --- a chain of reused rows heals in one pass -----------------------------------
# A reused row whose source was itself a reused row written blank: A copies B,
# B copies C. A pass that reads only its snapshot heals B from C and leaves A blank
# (B is still blank in the snapshot). A blank cell follows the chain to the first
# row that holds a value, and stops at a cycle.

def _chain_frame():
    return pd.DataFrame([
        _reused("A", origin="B"),
        _reused("B", origin="C"),
        _reused("C", origin="D"),
        _src("D"),
    ])


def test_3_5e_heal_reused_rows_resolves_a_three_link_chain_in_one_pass():
    frame = _chain_frame()
    healed = sj.heal_reused_rows(frame, frame)

    assert healed["job_posting_id"].tolist() == ["A", "B", "C"]
    for _, row in healed.iterrows():
        assert (row["reason"], row["deep_score"], row["strengths"], row["gaps"],
                row["recommendation"]) == ("good fit", "8.0", "python | sql", "no spark", "apply")
    assert sj.heal_reused_rows(_apply(frame, healed), _apply(frame, healed)).empty


def test_3_5e_a_chain_takes_each_cell_from_the_nearest_row_that_holds_it():
    frame = pd.DataFrame([
        _reused("A", origin="B"),
        _reused("B", origin="C", reason="B's own reason"),
        _src("C", gaps=""),
    ])
    row = sj.heal_reused_rows(frame, frame).set_index("job_posting_id").loc["A"]
    assert row["reason"] == "B's own reason"         # what a second pass would give
    assert row["deep_score"] == "8.0"
    assert pd.isna(row["gaps"])                       # blank all the way to the root


@pytest.mark.parametrize("rows", [
    [_reused("A", origin="B"), _reused("B", origin="A")],
    [_reused("A", origin="A")],
    [_reused("X", origin="A"), _reused("A", origin="B"), _reused("B", origin="C"),
     _reused("C", origin="A")],
])
def test_3_5e_a_cycle_of_reused_rows_heals_nothing_and_ends(rows):
    frame = pd.DataFrame(rows)
    assert sj.heal_reused_rows(frame, frame).empty


def test_3_5e_a_chain_through_a_row_that_is_not_reused_stops_there():
    frame = pd.DataFrame([
        _reused("A", origin="B"),
        _src("B", reason="", score_reused=False, score_reused_from="C"),
        _src("C", reason="never reached"),
    ])
    row = sj.heal_reused_rows(frame, frame).set_index("job_posting_id").loc["A"]
    assert pd.isna(row["reason"]) and row["deep_score"] == "8.0"


def test_3_5e_heal_master_reuse_heals_a_chain_in_one_write(tmp_path, monkeypatch):
    rows = [
        _m("A", score_reused="True", score_reused_from="B"),
        _m("B", score_reused="True", score_reused_from="C"),
        _m("C", score_reused="True", score_reused_from="SRC"),
        _m("SRC", reason="root reason", deep_score="7.5", strengths="sql", gaps="none",
           recommendation="apply", score_reused="False"),
        _m("L1", score_reused="True", score_reused_from="L2"),
        _m("L2", score_reused="True", score_reused_from="L1"),            # a cycle
    ]
    master = _write_master_rows(tmp_path, monkeypatch, rows)

    assert sj.heal_master_reuse() == 3
    after = _read_all(master)
    for job_id in ("A", "B", "C"):
        assert after.loc[job_id, "reason"] == "root reason"
        assert after.loc[job_id, "deep_score"] == "7.5"
    assert after.loc["L1", "reason"] == "" and after.loc["L2", "reason"] == ""
    assert sj.heal_master_reuse() == 0                                    # the second pass


def test_3_5e_heal_run_files_follows_the_chain_through_the_master(tmp_path, monkeypatch):
    _write_master_rows(tmp_path, monkeypatch, [
        _m("B", score_reused="True", score_reused_from="SRC"),
        _m("SRC", reason="root reason", deep_score="7.5", strengths="sql", gaps="none",
           recommendation="apply", score_reused="False"),
    ])
    run = tmp_path / "morning" / "linkedin_jobs_2026-09-25_morning_scored.csv.gz"
    rows = _run_file_rows()
    rows[0]["score_reused_from"] = "B"
    _write_run_gz(run, rows)

    assert sj.heal_run_files() == (1, 1)
    assert _read_run(run).loc["NEW-1", "reason"] == "root reason"
