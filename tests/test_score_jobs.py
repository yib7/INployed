import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import score_jobs as sj  # noqa: E402

from test_jobsdata_filter import REPOST_KEY_CASES  # noqa: E402


# P2-8: both scorer system prompts must tell the model the job description is
# untrusted data and not to follow instructions embedded in it. Guards against a
# hostile posting ("score this 5 / recommend apply") and against silent removal
# of the defensive sentence.

def test_stage_system_prompts_flag_job_description_as_untrusted():
    for prompt in (sj.STAGE1_SYSTEM, sj.STAGE2_SYSTEM):
        low = prompt.lower()
        assert "untrusted data" in low
        assert "ignore any instructions" in low


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
        # `model` is the stage's ranked chain, as KeyPool.generate takes it.
        model = model[0] if isinstance(model, (list, tuple)) else model
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


def _default_render(template):
    """The template as the model reads it at the default candidate profile."""
    return template.format(resume="RESUME", job="JOB", today="TODAY",
                           **sj.candidate_prompt_vars(sj.candidate_profile()))


def test_stage1_template_ignores_geography_and_workauth():
    """A1: Stage 1 must explicitly ignore location/relocation/work-auth so JD text
    can't implicitly dock onsite/relocation roles."""
    t = _default_render(sj.STAGE1_TEMPLATE).lower()
    assert "ignore completely" in t
    for kw in ("relocat", "onsite", "remote", "time zone", "work authorization"):
        assert kw in t, kw


def test_stage2_template_excludes_location_and_workauth_gaps():
    """A1: Stage 2 must not list location/relocation/work-auth as a gap."""
    t = _default_render(sj.STAGE2_TEMPLATE).lower()
    assert "never list location" in t
    for kw in ("relocat", "work authorization", "sponsorship"):
        assert kw in t, kw


def test_templates_anchor_current_date():
    """Both prompts must carry a {today} placeholder plus wording that pins all
    resume/JD dates to it: the models' training data predates the candidate's
    May 2026 graduation, so without an explicit current date they read it as
    upcoming ("hasn't graduated yet") and dock the score."""
    for tmpl in (sj.STAGE1_TEMPLATE, sj.STAGE2_TEMPLATE):
        t = _default_render(tmpl).lower()
        assert "{today}" in tmpl
        assert "today's date" in t
        assert "training data" in t


def test_score_prompts_include_todays_date():
    """The prompt actually sent to the model must contain the real current date."""
    pool = FakePool({})
    asyncio.run(sj.score_stage1(pool, asyncio.Semaphore(1), "resume", "J1", "jd"))
    asyncio.run(sj.score_stage2(pool, asyncio.Semaphore(1), "resume", "J2", "jd"))
    assert len(pool.calls) == 2
    for _, contents in pool.calls:
        assert sj.today_str() in contents


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
                        classmethod(lambda cls, *, state_path, limits=None: sentinel))
    assert sj.make_pool() is sentinel


def test_make_pool_passes_the_configured_rate_limits_through(monkeypatch):
    """The limits must actually REACH the pool. Every _limits_for test would still
    pass if make_pool quietly dropped them, and the symptom -- a slow run that
    spills onto paid Vertex -- looks identical to having no limits configured."""
    seen = {}

    def _capture(cls, *, state_path, limits=None):
        seen["limits"] = limits
        return object()

    monkeypatch.setattr(sj.KeyPool, "from_env", classmethod(_capture))
    monkeypatch.setattr(sj, "_SCORING", dict(sj._SCORING, stage1_model="m1",
                                             model_limits=["m1 15 500"]))
    monkeypatch.setattr(sj, "STAGE1_MODEL", "m1")
    sj.make_pool()
    assert seen["limits"] == {"m1": {"rpm": 15, "rpd": 500}}


def test_make_pool_warns_when_a_model_has_no_limits_anywhere(monkeypatch, capsys):
    """The silent downgrade has to announce itself -- that is the whole incident."""
    monkeypatch.setattr(sj.KeyPool, "from_env",
                        classmethod(lambda cls, *, state_path, limits=None: object()))
    monkeypatch.setattr(sj, "_SCORING", dict(sj._SCORING, model_limits=[]))
    monkeypatch.setattr(sj, "STAGE1_MODELS", ["gemini-9.9-unheard-of"])
    sj.make_pool()
    out = capsys.readouterr().out
    assert "gemini-9.9-unheard-of" in out
    assert "Settings -> Scoring" in out


def test_make_pool_warns_for_an_unlimited_fallback_model(monkeypatch, capsys):
    """A fallback never gets its own rpm/rpd boxes, so an unknown one is exactly
    the silent-downgrade case the warning exists for -- the chain must be walked
    whole, not just its primary."""
    monkeypatch.setattr(sj.KeyPool, "from_env",
                        classmethod(lambda cls, *, state_path, limits=None: object()))
    monkeypatch.setattr(sj, "_SCORING", dict(sj._SCORING, model_limits=[]))
    monkeypatch.setattr(sj, "STAGE2_MODELS",
                        ["gemini-3.5-flash", "gemini-9.9-unheard-of"])
    sj.make_pool()
    assert "gemini-9.9-unheard-of" in capsys.readouterr().out


def test_append_run_stats_migrates_old_header(tmp_path, monkeypatch):
    import csv as _csv
    old = tmp_path / "run_stats.csv"
    # header before free_calls, vertex_calls, easy_apply_dropped, scores_reused and the jev_* columns
    old_cols = sj.RUN_STATS_COLS[:sj.RUN_STATS_COLS.index("free_calls")]
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


# P2-6: a corrupt-but-present master must raise an OSError naming the fix (fix/restore),
# not a raw pandas ParserError/UnicodeDecodeError out of save_output -> main after
# the scored gz is already written. Mirrors scraper.append_to_master's guard.

def test_update_master_scores_unreadable_master_raises_actionable_oserror(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_bytes(b"\x00\x01\x02not,a\ncsv,file,with,too,many,fields\n\xff\xfe\n")
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5, "recommendation": "apply"}])
    with pytest.raises(OSError) as exc:
        sj.update_master_scores(scored)

    # A recovery message that names the fix, NOT a raw pandas parse traceback.
    assert not isinstance(exc.value, pd.errors.ParserError)
    msg = str(exc.value)
    assert "unreadable" in msg
    assert "linkedin_jobs_master.csv" in msg
    # no stray tmp file left behind in the master's directory
    leftovers = [p for p in tmp_path.iterdir() if p.name != "linkedin_jobs_master.csv"]
    assert leftovers == []


def test_update_master_scores_corrupt_row_midstream_raises_actionable_oserror(tmp_path, monkeypatch):
    # Header parses fine (nrows=0 read passes) but a row deep in the stream has
    # the wrong field count -- the ParserError only surfaces during the chunked
    # read loop, which must still be converted to that same OSError.
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_text("job_posting_id,job_title\n1,A\n2,B,EXTRA,FIELDS\n", encoding="utf-8")
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    scored = pd.DataFrame([{"job_posting_id": "1", "score": 5}])
    with pytest.raises(OSError) as exc:
        sj.update_master_scores(scored)
    assert not isinstance(exc.value, pd.errors.ParserError)
    assert "unreadable" in str(exc.value)


# P2-11: historical filtered_out spellings from a float upcast ("1.0") or an
# older writer ("True " with a trailing space) must read as ALREADY-filtered so
# the rescore pass never retries them forever, burning RESCORE_CAP slots.

def test_rows_needing_rescore_treats_float_and_padded_filtered_out_as_filtered():
    master = pd.DataFrame([
        {"job_posting_id": "1", "score": "", "filtered_out": "1.0", "reason": ""},
        {"job_posting_id": "2", "score": "", "filtered_out": "True ", "reason": ""},
        {"job_posting_id": "3", "score": "", "filtered_out": "False", "reason": ""},
    ])
    out_ids = set(sj.rows_needing_rescore(master)["job_posting_id"])
    assert "1" not in out_ids   # "1.0" (float upcast) -> filtered, never retried
    assert "2" not in out_ids   # "True " (trailing space) -> filtered, never retried
    assert "3" in out_ids       # genuinely unfiltered + unscored -> needs rescore


# P2-6: SCORE_COLS must fold ALL mechanical-filter columns into the master, not
# just a subset -- else the master's filter record is partial/inconsistent.

def test_score_cols_include_all_filter_columns():
    for col in ("filter_junk_title", "filter_junk_desc", "filter_too_many_years",
               "filter_clearance", "filter_degree", "filter_easy_apply", "filter_internship",
               "filtered_out"):
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
        "filter_internship": True,
        "filtered_out": True,
    }])
    sj.update_master_scores(scored)

    out = pd.read_csv(master, dtype={"job_posting_id": str})
    row1 = out[out["job_posting_id"] == "1"].iloc[0]
    assert bool(row1["filter_junk_desc"]) is True
    assert bool(row1["filter_clearance"]) is True
    assert bool(row1["filter_degree"]) is False
    assert bool(row1["filter_internship"]) is True


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


# P2-1: save_output's *_scored.csv.gz write must be atomic. This is the one with
# teeth: latest_input_csv() skips any input whose _scored.csv.gz merely EXISTS, so
# a truncated gz left by a crashed naked write hides that input forever (and every
# dashboard/watcher read of the gz then fails). Atomic = a crash leaves either the
# whole old file or the whole new one, never a truncated partial at the final path.

def test_save_output_scored_gz_routes_through_atomic_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")  # no master -> merge no-ops
    input_csv = tmp_path / "linkedin_jobs_2026-07-01_morning.csv"
    input_csv.write_text("job_posting_id\n1\n", encoding="utf-8")

    calls = []
    real = sj._atomic_to_csv

    def spy(df, path, **kwargs):
        calls.append((Path(path), kwargs))
        return real(df, path, **kwargs)

    monkeypatch.setattr(sj, "_atomic_to_csv", spy)
    out_path = sj.save_output(pd.DataFrame([{"job_posting_id": "1", "score": 5}]), input_csv)

    # exactly the scored gz went through the atomic helper, with compression="gzip"
    # reaching to_csv (helper forwards **kwargs), and it round-trips as real gzip.
    assert (out_path, {"compression": "gzip"}) in calls
    round_tripped = pd.read_csv(out_path, dtype={"job_posting_id": str}, compression="gzip")
    assert round_tripped.iloc[0]["score"] == 5


def test_save_output_crash_leaves_no_scored_gz_so_input_reoffered(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(sj, "RUN_LABELS", ["morning"])
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    run_dir = tmp_path / "morning"
    run_dir.mkdir()
    input_csv = run_dir / "linkedin_jobs_2026-07-01_morning.csv"
    input_csv.write_text("job_posting_id\n1\n", encoding="utf-8")

    assert sj.latest_input_csv() == input_csv  # unscored input is offered

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")

    monkeypatch.setattr(os, "replace", boom_replace)
    with pytest.raises(OSError):
        sj.save_output(pd.DataFrame([{"job_posting_id": "1", "score": 5}]), input_csv)

    out_path = input_csv.with_name(input_csv.stem + "_scored.csv.gz")
    assert not out_path.exists()                                    # no truncated gz stranded
    assert [p for p in run_dir.iterdir() if p.suffix == ".tmp"] == []
    assert sj.latest_input_csv() == input_csv                       # input re-offered, not skipped


def test_append_run_stats_self_heal_rewrite_is_atomic(tmp_path, monkeypatch):
    import csv as _csv
    old = tmp_path / "run_stats.csv"
    old_cols = sj.RUN_STATS_COLS[:-2]  # older/narrower header -> triggers the self-heal rewrite
    with open(old, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=old_cols)
        w.writeheader()
        w.writerow({c: 1 for c in old_cols})
    before = old.read_bytes()
    monkeypatch.setattr(sj, "RUN_STATS_CSV", old)

    def boom_replace(*a, **k):
        raise OSError("simulated crash mid self-heal rewrite")

    monkeypatch.setattr(os, "replace", boom_replace)
    # append_run_stats swallows OSError (stats bookkeeping never kills a run); the
    # point here is the REWRITE is atomic -- it streams to a same-dir tmp then
    # os.replace, so a crash leaves the existing file whole, never truncated.
    sj.append_run_stats({c: 2 for c in sj.RUN_STATS_COLS})

    assert old.read_bytes() == before                              # untouched: replace never landed
    assert [p for p in tmp_path.iterdir() if p.name != "run_stats.csv"] == []


# --- SP6: score-side repost reuse -------------------------------------------------
#
# pipeline/score_jobs.py is copied standalone to the VM (no local/ package), so it
# carries its OWN self-contained repost_key. The table below (shared with
# tests/test_jobsdata_filter.py) pins the two copies to agreeing on every row
# so they can't quietly drift apart.

def test_pipeline_repost_key_matches_jobsdata_repost_key():
    import jobsdata
    for title, company, location, _expected in REPOST_KEY_CASES:
        assert sj.repost_key(title, company, location) == jobsdata.repost_key(
            title, company, location)


def test_repost_fingerprint_empty_key_is_never_reused():
    # No company -> repost_key is "" -> the fingerprint must also be "", so an
    # empty key can only ever produce an empty fingerprint that never matches.
    assert sj.repost_fingerprint("Data Engineer", "", "Seattle, WA", "some jd text") == ""


def test_repost_fingerprint_changes_with_description():
    fp1 = sj.repost_fingerprint("Data Engineer", "Acme", "Seattle, WA", "Build pipelines.")
    fp2 = sj.repost_fingerprint("Data Engineer", "Acme", "Seattle, WA", "Ship dashboards.")
    assert fp1 != fp2


def test_repost_fingerprint_stable_for_identical_inputs():
    fp1 = sj.repost_fingerprint("Data Engineer", "Acme", "Seattle, WA", "Build pipelines.")
    fp2 = sj.repost_fingerprint("Data Engineer", "Acme", "Seattle, WA", "Build pipelines.")
    assert fp1 == fp2 and fp1 != ""


def _master_row(job_id, days_ago, today, **overrides):
    row = {
        "job_posting_id": job_id,
        "job_title": "Data Engineer",
        "company_name": "Acme",
        "job_location": "Seattle, WA",
        "job_description_md": "Build data pipelines end to end.",
        "score": 5,
        "reason": "good fit",
        "deep_score": 8,
        "strengths": "python",
        "gaps": "",
        "recommendation": "apply",
        "extracted_date": (today - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
    }
    row.update(overrides)
    return row


def _fresh_row(job_id, **overrides):
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


def test_reuse_repost_scores_hit_copies_six_columns_and_sets_reused_from():
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    df = pd.DataFrame([_fresh_row("NEW-1")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 1
    row = out.iloc[0]
    assert row["score"] == 5
    assert row["deep_score"] == 8
    assert row["reason"] == "good fit"
    assert row["strengths"] == "python"
    assert row["gaps"] == ""
    assert row["recommendation"] == "apply"
    assert row["score_reused_from"] == "OLD-1"
    assert bool(row["score_reused"]) is True


def test_reuse_repost_scores_two_reposts_of_one_master_job_both_reuse(capsys):
    # Two DIFFERENT new postings that both happen to fingerprint-match the
    # same master row (e.g. the same listing reposted twice by the company
    # before this run scored either): both must reuse independently. Matching
    # is a repeatable lookup against the master, so a second row with the
    # same fingerprint still finds it.
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    df = pd.DataFrame([_fresh_row("NEW-1"), _fresh_row("NEW-2")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 2
    assert "Reposts: reused 2 scores" in capsys.readouterr().out
    for _, row in out.iterrows():
        assert row["score"] == 5
        assert row["score_reused_from"] == "OLD-1"
        assert bool(row["score_reused"]) is True


def test_reuse_repost_scores_miss_when_master_score_is_blank():
    # A fingerprint match against a master row that was never actually scored
    # (blank/NaN score, e.g. a prior run's spend-cap overflow) must not
    # "reuse" a score that doesn't exist -- the row goes to the pool as usual.
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today, score=float("nan"))])
    df = pd.DataFrame([_fresh_row("NEW-1")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False
    assert pd.isna(out.iloc[0]["score_reused_from"])


def test_run_scoring_skips_pool_for_two_reposts_of_one_master_job():
    """The pool must receive zero calls for EITHER reused row."""
    reused_cols = {
        "score": 5, "reason": "good fit", "deep_score": 8,
        "strengths": "python", "gaps": "", "recommendation": "apply",
    }
    df = pd.DataFrame([
        {"job_posting_id": "NEW-1", "job_description_md": "reused job one",
         "filtered_out": False, "score_reused": True, "score_reused_from": "OLD-1",
         **reused_cols},
        {"job_posting_id": "NEW-2", "job_description_md": "reused job two",
         "filtered_out": False, "score_reused": True, "score_reused_from": "OLD-1",
         **reused_cols},
    ])
    pool = FakePool({})

    merged = asyncio.run(sj.run_scoring(pool, "resume", df))

    assert pool.calls == []   # zero calls for BOTH reused rows
    for _, row in merged.iterrows():
        assert row["score"] == 5
        assert row["score_reused_from"] == "OLD-1"


def test_reuse_repost_scores_miss_on_changed_description():
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    df = pd.DataFrame([_fresh_row("NEW-1", job_description_md="A completely different role.")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False
    assert pd.isna(out.iloc[0]["score_reused_from"])


def test_reuse_repost_scores_miss_on_same_id():
    # A row already IN the master (a re-scrape of the same posting) must not
    # "reuse" its own prior score through this path -- update_master_scores'
    # normal fold already handles that case.
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("SAME-1", 5, today)])
    df = pd.DataFrame([_fresh_row("SAME-1")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False


def test_reuse_repost_scores_off_when_reuse_days_is_zero():
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    df = pd.DataFrame([_fresh_row("NEW-1")])

    out, n = sj.reuse_repost_scores(df, master, 0, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False


def test_reuse_repost_scores_window_edge_30_in_31_out():
    today = date(2026, 9, 19)
    master_in = pd.DataFrame([_master_row("OLD-1", 30, today)])
    master_out = pd.DataFrame([_master_row("OLD-1", 31, today)])
    df = pd.DataFrame([_fresh_row("NEW-1")])

    _, n_in = sj.reuse_repost_scores(df.copy(), master_in, 30, today=today)
    _, n_out = sj.reuse_repost_scores(df.copy(), master_out, 30, today=today)

    assert n_in == 1
    assert n_out == 0


def test_reuse_repost_scores_missing_master_yields_no_reuse_and_no_crash():
    today = date(2026, 9, 19)
    df = pd.DataFrame([_fresh_row("NEW-1")])

    out, n = sj.reuse_repost_scores(df, None, 30, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False

    out2, n2 = sj.reuse_repost_scores(df, pd.DataFrame(), 30, today=today)
    assert n2 == 0


def test_reuse_repost_scores_prints_the_count_only_when_positive(capsys):
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    df = pd.DataFrame([_fresh_row("NEW-1")])

    sj.reuse_repost_scores(df, master, 30, today=today)
    assert "Reposts: reused 1 scores" in capsys.readouterr().out

    df_miss = pd.DataFrame([_fresh_row("NEW-2", job_description_md="totally different")])
    sj.reuse_repost_scores(df_miss, master, 30, today=today)
    assert "Reposts" not in capsys.readouterr().out


def test_run_scoring_skips_pool_for_reused_rows_and_keeps_their_scores():
    """The checkpoint case: a reused row's six columns must survive run_scoring's
    stage-1/stage-2 merge, and the mocked pool must receive ZERO calls for it."""
    df = pd.DataFrame([
        {
            "job_posting_id": "NEW-1", "job_description_md": "reused job",
            "filtered_out": False, "score_reused": True, "score_reused_from": "OLD-1",
            "score": 5, "reason": "good fit", "deep_score": 8,
            "strengths": "python", "gaps": "", "recommendation": "apply",
        },
        {
            "job_posting_id": "NEW-2", "job_description_md": "fresh job",
            "filtered_out": False, "score_reused": False, "score_reused_from": pd.NA,
            "score": pd.NA, "reason": pd.NA, "deep_score": pd.NA,
            "strengths": pd.NA, "gaps": pd.NA, "recommendation": pd.NA,
        },
    ])
    pool = FakePool({"fresh job": 3})

    merged = asyncio.run(sj.run_scoring(pool, "resume", df))

    for _, contents in pool.calls:
        assert "reused job" not in contents   # zero calls for the reused row

    reused_row = merged[merged["job_posting_id"] == "NEW-1"].iloc[0]
    assert reused_row["score"] == 5
    assert reused_row["deep_score"] == 8
    assert reused_row["reason"] == "good fit"
    assert reused_row["recommendation"] == "apply"

    fresh_row = merged[merged["job_posting_id"] == "NEW-2"].iloc[0]
    assert fresh_row["score"] == 3


def test_reused_row_lands_in_save_output_scored_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(sj, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    input_csv = tmp_path / "linkedin_jobs_2026-09-19_morning.csv"
    input_csv.write_text("job_posting_id\nNEW-1\n", encoding="utf-8")

    df = pd.DataFrame([{
        "job_posting_id": "NEW-1", "score": 5, "score_reused": True,
        "score_reused_from": "OLD-1",
    }])
    out_path = sj.save_output(df, input_csv)

    out = pd.read_csv(out_path, dtype={"job_posting_id": str}, compression="gzip")
    assert out.iloc[0]["score"] == 5
    assert out.iloc[0]["score_reused_from"] == "OLD-1"


def test_rows_needing_rescore_ignores_a_reused_row_with_a_copied_score():
    master = pd.DataFrame([
        {"job_posting_id": "NEW-1", "score": 5, "filtered_out": "False", "reason": "good fit",
         "recommendation": "apply", "score_reused": "True", "score_reused_from": "OLD-1"},
        {"job_posting_id": "NEW-2", "score": "", "filtered_out": "False", "reason": ""},
    ])
    out_ids = set(sj.rows_needing_rescore(master)["job_posting_id"])
    assert "NEW-1" not in out_ids  # has a real copied score -> not a rescore candidate
    assert "NEW-2" in out_ids


# P2-fix-15: a reused row must never chain-provide a score to a third
# repost -- a row's score is only ever trustworthy back to a row the model
# actually scored, so `score_reused` must exclude reused rows from the
# candidate set on the master side of the lookup.

def test_reuse_repost_scores_does_not_chain_through_a_reused_row():
    # A scored day 0. B (day 25) reused from A. By day 50, A is out of the
    # 30-day window and B only carries a copied score (score_reused=True), so
    # C must find nothing to reuse and goes to the pool.
    today = date(2026, 9, 19)
    master = pd.DataFrame([
        _master_row("OLD-A", 50, today, score_reused=False, score_reused_from=pd.NA),
        _master_row("OLD-B", 25, today, score_reused=True, score_reused_from="OLD-A"),
    ])
    df = pd.DataFrame([_fresh_row("NEW-C")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 0
    assert bool(out.iloc[0]["score_reused"]) is False
    assert pd.isna(out.iloc[0]["score_reused_from"])


def test_reuse_repost_scores_still_works_against_an_older_master_schema():
    # A master written before score_reused existed has no such column at all
    # -- must not crash, and a genuinely-scored row must still be reusable.
    today = date(2026, 9, 19)
    master = pd.DataFrame([_master_row("OLD-1", 5, today)])
    assert "score_reused" not in master.columns
    df = pd.DataFrame([_fresh_row("NEW-1")])

    out, n = sj.reuse_repost_scores(df, master, 30, today=today)

    assert n == 1
    assert out.iloc[0]["score_reused_from"] == "OLD-1"


def test_load_master_for_reuse_loads_score_reused_when_present(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{
        "job_posting_id": "OLD-1", "score": 5, "extracted_date": "2026-09-14",
        "job_title": "Data Engineer", "company_name": "Acme", "job_location": "Seattle, WA",
        "job_description_md": "Build pipelines.", "score_reused": True,
    }]).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    out = sj.load_master_for_reuse()

    assert out is not None
    assert "score_reused" in out.columns
    assert bool(out.iloc[0]["score_reused"]) is True


def test_load_master_for_reuse_tolerates_a_master_without_score_reused(tmp_path, monkeypatch):
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{
        "job_posting_id": "OLD-1", "score": 5, "extracted_date": "2026-09-14",
        "job_title": "Data Engineer", "company_name": "Acme", "job_location": "Seattle, WA",
        "job_description_md": "Build pipelines.",
    }]).to_csv(master, index=False)
    monkeypatch.setattr(sj, "MASTER_CSV", master)

    out = sj.load_master_for_reuse()

    assert out is not None
    assert "score_reused" not in out.columns


def test_restore_reused_scores_drops_a_duplicated_job_posting_id():
    # A duplicated job_posting_id in the reused snapshot must not raise on the
    # `.loc[snap.index] = ...` write -- keep the first occurrence only.
    result = pd.DataFrame([
        {"job_posting_id": "NEW-1", "score": None},
        {"job_posting_id": "NEW-2", "score": None},
    ])
    reused_snapshot = pd.DataFrame([
        {"job_posting_id": "NEW-1", "score": 5},
        {"job_posting_id": "NEW-1", "score": 9},   # duplicate id, different value
    ])

    out = sj._restore_reused_scores(result, reused_snapshot)

    row1 = out[out["job_posting_id"] == "NEW-1"].iloc[0]
    assert row1["score"] == 5   # first occurrence wins
    assert len(out) == 2


def test_repost_reuse_days_config_default_and_disable(monkeypatch, tmp_path):
    monkeypatch.delenv("SCORE_REPOST_REUSE_DAYS", raising=False)
    monkeypatch.setattr(sj, "OUTPUT_DIR", tmp_path)
    cfg = sj.load_scoring_config()
    assert cfg["repost_reuse_days"] == 30

    monkeypatch.setenv("SCORE_REPOST_REUSE_DAYS", "0")
    cfg = sj.load_scoring_config()
    assert cfg["repost_reuse_days"] == 0
