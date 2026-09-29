"""Repost score reuse must carry all six score columns, and reused rows that an
earlier build wrote blank are healed from their source row.

Cycle 21. Since 0b0d664 `reuse_repost_scores` copied `_REPOST_REUSE_COLS`
(score, reason, deep_score, strengths, gaps, recommendation) from a master row,
but `load_master_for_reuse` projected only the identity columns plus `score`, so
a reused repost got a real score and a blank reason, deep_score, strengths, gaps
and recommendation. Task 1 fixes the loader; Task 2 heals the rows already written.

Every test points the master and the run folders at tmp_path.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

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


# --- Task 1: the loader reads all six reuse columns ------------------------------

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
    import asyncio

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
