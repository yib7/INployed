import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import score_jobs as sj  # noqa: E402


def _resp(text):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
    )


class FakePool:
    def __init__(self, stage1_by_substr):
        self.stage1 = stage1_by_substr
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents))
        if model == sj.STAGE1_MODEL:
            score = 1
            for sub, sc in self.stage1.items():
                if sub in contents:
                    score = sc
                    break
            return _resp(json.dumps({"score": score, "reason": "r"}))
        return _resp(json.dumps(
            {"deep_score": 8, "strengths": ["s"], "gaps": ["g"], "recommendation": "apply"}))

    def stats(self):
        return {"free_calls": len(self.calls), "vertex_calls": 0}


def test_stage1_template_ignores_geography_and_workauth():
    """A1: Stage 1 must explicitly ignore location/relocation/work-auth so JD text
    can't implicitly dock onsite/relocation roles."""
    t = sj.STAGE1_TEMPLATE.lower()
    assert "ignore completely" in t
    for kw in ("relocat", "onsite", "remote", "time zone", "work authorization"):
        assert kw in t, kw


def test_stage2_template_excludes_location_and_workauth_gaps():
    """A1: Stage 2 must not list location/relocation/work-auth as a gap."""
    t = sj.STAGE2_TEMPLATE.lower()
    assert "never list location" in t
    for kw in ("relocat", "work authorization", "sponsorship"):
        assert kw in t, kw


def test_score_stage1_success():
    pool = FakePool({"JD-TEXT": 5})
    out = asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "resume", "J1", "JD-TEXT here"))
    assert out == {"job_posting_id": "J1", "score": 5, "reason": "r"}


def test_score_stage1_error_returns_error_dict():
    class Boom:
        async def generate(self, **k):
            raise RuntimeError("kaboom")
    out = asyncio.run(sj.score_stage1(Boom(), asyncio.Semaphore(1), "resume", "J1", "x"))
    assert out["score"] is None
    assert out["reason"].startswith("ERROR:")


def test_stage2_dispatched_highest_score_first(monkeypatch):
    monkeypatch.setattr(sj, "STAGE2_CONCURRENCY", 1)
    df = pd.DataFrame({
        "job_posting_id": ["j1", "j2", "j3"],
        "job_description_md": ["AAA", "BBB", "CCC"],
        "filtered_out": [False, False, False],
    })
    pool = FakePool({"AAA": 5, "BBB": 4, "CCC": 5})
    asyncio.run(sj.run_scoring(pool, "resume", df))
    order = []
    for model, contents in pool.calls:
        if model == sj.STAGE2_MODEL:
            for sub in ("AAA", "BBB", "CCC"):
                if sub in contents:
                    order.append(sub)
    # AAA before CCC: stable sort keeps original order among equal (score-5) jobs
    assert order == ["AAA", "CCC", "BBB"]


def test_make_pool_delegates(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(sj.KeyPool, "from_env",
                        classmethod(lambda cls, *, state_path: sentinel))
    assert sj.make_pool() is sentinel


def test_append_run_stats_migrates_old_header(tmp_path, monkeypatch):
    import csv as _csv
    old = tmp_path / "run_stats.csv"
    old_cols = sj.RUN_STATS_COLS[:-2]  # header before free_calls/vertex_calls were added
    with open(old, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=old_cols)
        w.writeheader()
        w.writerow({c: 1 for c in old_cols})
    monkeypatch.setattr(sj, "RUN_STATS_CSV", old)

    sj.append_run_stats({c: 2 for c in sj.RUN_STATS_COLS})

    df = pd.read_csv(old)
    assert list(df.columns) == sj.RUN_STATS_COLS  # uniform width, pandas-readable
    assert len(df) == 2
    assert df.iloc[0]["free_calls"] == 0   # old row backfilled
    assert df.iloc[1]["free_calls"] == 2   # new row written


# P1-2: score_jobs.py is copied standalone to the VM, so it gets its own private
# _atomic_to_csv (content correctness + tmp cleanup on failure), and
# update_master_scores must use it so a crash mid-write never truncates the master.

def test_atomic_to_csv_writes_correct_content_and_replaces_file(tmp_path):
    path = tmp_path / "out.csv"
    df = pd.DataFrame([{"job_posting_id": "1", "score": 5},
                       {"job_posting_id": "2", "score": 3}])
    sj._atomic_to_csv(df, path)
    round_tripped = pd.read_csv(path, dtype={"job_posting_id": str})
    assert list(round_tripped["job_posting_id"]) == ["1", "2"]
    assert list(round_tripped["score"]) == [5, 3]
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []


def test_atomic_to_csv_cleans_up_tmp_on_failure_and_leaves_target_untouched(monkeypatch, tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("job_posting_id,score\n1,5\n", encoding="utf-8")
    before = path.read_bytes()

    def boom(self, *a, **k):
        raise ValueError("kaboom mid-write")
    monkeypatch.setattr(pd.DataFrame, "to_csv", boom)

    df = pd.DataFrame([{"job_posting_id": "2", "score": 3}])
    with pytest.raises(ValueError):
        sj._atomic_to_csv(df, path)

    assert path.read_bytes() == before
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []


def test_update_master_scores_writes_atomically_and_correctly(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                 {"job_posting_id": "2", "job_title": "B"}]).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5, "recommendation": "apply"}])
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str})
    row1 = out[out["job_posting_id"] == "1"].iloc[0]
    assert row1["score"] == 5
    assert row1["recommendation"] == "apply"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "linkedin_jobs_master.csv"]
    assert leftovers == []  # no stray tmp file left in the master's directory


def test_update_master_scores_leaves_master_untouched_on_replace_failure(tmp_path, monkeypatch):
    # A crash mid-write (disk full, kill, OOM) must never truncate the cumulative
    # master -- the final write must go through _atomic_to_csv (tmp + os.replace),
    # not a naked to_csv straight onto MASTER_CSV. Failing os.replace AFTER the tmp
    # file is fully written proves the real destination was never opened for write
    # (a naked to_csv would have already truncated/replaced MASTER_CSV by now).
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                 {"job_posting_id": "2", "job_title": "B"}]).to_csv(master, index=False)
    before = master.read_bytes()
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(os, "replace", boom_replace)

    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5, "recommendation": "apply"}])
    with pytest.raises(OSError):
        sj.update_master_scores(scored)

    assert master.read_bytes() == before            # untouched: os.replace never landed


# P2-6: SCORE_COLS must fold ALL mechanical-filter columns into the master, not
# just a subset -- else the master's filter record is partial/inconsistent.

def test_score_cols_include_all_filter_columns():
    for col in ("filter_junk_title", "filter_junk_desc", "filter_too_many_years",
               "filter_clearance", "filter_degree", "filtered_out"):
        assert col in sj.SCORE_COLS, col


def test_update_master_scores_folds_all_filter_columns_into_master(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"}]).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    scored = pd.DataFrame([{
        "job_posting_id": "1",
        "filter_junk_title": False,
        "filter_junk_desc": True,
        "filter_too_many_years": False,
        "filter_clearance": True,
        "filter_degree": False,
        "filtered_out": True,
    }])
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str})
    row1 = out[out["job_posting_id"] == "1"].iloc[0]
    assert bool(row1["filter_junk_desc"]) is True
    assert bool(row1["filter_clearance"]) is True
    assert bool(row1["filter_degree"]) is False


# P2-11: re-scoring a fresh scrape must NOT reset an existing master row's
# is_seen back to "no" -- the master merge must drop is_seen the same way
# rescore_master_failures already does, while the per-run scored CSV output
# still carries is_seen (for the local sticky-registry reconcile).

def test_update_master_scores_never_touches_is_seen_in_master(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A", "is_seen": "yes"}]).to_csv(
        master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    # Simulates a fresh-scrape rescoring pass: whole frame carries is_seen="no"
    # (save_output's happy-path behavior) alongside a real score update.
    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5, "is_seen": "no"}])
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str})
    row1 = out[out["job_posting_id"] == "1"].iloc[0]
    assert row1["score"] == 5               # the real update still lands
    assert row1["is_seen"] == "yes"         # but is_seen in the master is untouched


def test_save_output_scored_csv_still_carries_is_seen(tmp_path, monkeypatch):
    # The MASTER merge drops is_seen, but the per-run scored CSV (consumed by the
    # local sticky-registry reconcile) must still have the column.
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")  # no master -> merge no-ops
    input_csv = tmp_path / "linkedin_jobs_2026-07-01_morning.csv"
    input_csv.write_text("job_posting_id\n1\n", encoding="utf-8")

    df = pd.DataFrame([{"job_posting_id": "1", "score": 5}])
    out_path = sj.save_output(df, input_csv)

    out = pd.read_csv(out_path, dtype={"job_posting_id": str}, compression="gzip")
    assert "is_seen" in out.columns
    assert out.iloc[0]["is_seen"] == "no"


# P2-12: a missing resume.md must exit with a friendly message, not a raw
# FileNotFoundError traceback.

def test_load_resume_missing_file_exits_with_friendly_message(monkeypatch, tmp_path):
    monkeypatch.setattr(sj, "RESUME_PATH", tmp_path / "resume.md")
    with pytest.raises(SystemExit) as exc_info:
        sj.load_resume()
    msg = str(exc_info.value)
    assert "resume.md" in msg
    assert "Resume Data" in msg


def test_load_resume_reads_existing_file(monkeypatch, tmp_path):
    resume_path = tmp_path / "resume.md"
    resume_path.write_text("# My Resume\n", encoding="utf-8")
    monkeypatch.setattr(sj, "RESUME_PATH", resume_path)
    assert sj.load_resume() == "# My Resume\n"
