"""scripts/jev_score_calibrate.py (VL-2): the Jev scorer's calibration run.

Hermetic: a synthetic master and résumé in tmp_path, `live_judge` swapped for
`jev.DryRun(jev.FakeJev())` (it counts each request like a live one, so the
spend cap works, and makes none), and the cache directory moved into tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

import jev
import jev_score

REPO = Path(__file__).resolve().parent.parent


def _load():
    name = "jev_score_calibrate"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


calib = _load()

RESUME_MARKER = "RESUMEMARKER7731"
TEXT_MARKER = "JOBTEXTMARKER"


def _desc(i: int) -> str:
    return (f"<p>Synthetic role {i}. {TEXT_MARKER}{i:03d}</p>"
            "<p><strong>Requirements</strong></p><ul>"
            "<li>Experience with Python and SQL for data analysis</li>"
            "<li>Build dashboards in Tableau or Power BI</li>"
            "<li>Knowledge of statistics and A/B testing</li>"
            "<li>Communicate findings to stakeholders</li></ul>"
            + "<p>The team analyzes product data and reports on it every week.</p>" * 3)


def _row(i: int, gemini: int, **extra) -> dict:
    row = {"job_posting_id": f"40000{i:03d}", "job_title": f"Synthetic Analyst {i}",
           "score": str(gemini), "deep_score": "8" if gemini >= 4 else "",
           "recommendation": "apply" if gemini >= 4 else "", "reason": "gemini reason",
           "filtered_out": "False", "score_reused": "False",
           "job_description_formatted": _desc(i),
           "job_summary": f"Plain summary {TEXT_MARKER}{i:03d}. " * 12}
    row.update(extra)
    return row


def _write_master(path: Path, rows) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A synthetic master (8 jobs per Gemini score plus rows to leave out), a
    résumé, the cache in tmp_path and the fake judge."""
    rows = [_row(i, 1 + i % 5) for i in range(40)]
    rows += [_row(900, 3, job_posting_id="manual-1a2b"), _row(901, 3, filtered_out="True"),
             _row(902, 3, score_reused="1.0"), _row(903, 3, reason="ERROR: boom"),
             _row(904, 3, reason="Skills fit strong (0.90); domain data analytics"),
             _row(905, 3, score=""), _row(906, 3, job_description_formatted="", job_summary=""),
             _row(0, 5)]                            # a second row for job 0: the first row wins
    master = _write_master(tmp_path / "master.csv", rows)
    resume = tmp_path / "resume.md"
    resume.write_text(f"# Synthetic Candidate\n{RESUME_MARKER}\nPython, SQL, Tableau, "
                      "statistics, dashboards, A/B testing, stakeholders.\n", encoding="utf-8")
    cache = tmp_path / "cache"
    monkeypatch.setattr(calib, "CACHE_DIR", cache)
    monkeypatch.setattr(calib, "live_judge", lambda: jev.DryRun(jev.FakeJev()))
    return {"master": master, "resume": resume, "cache": cache,
            "args": ["--master", str(master), "--resume", str(resume)]}


def _no_text(out: str) -> None:
    assert RESUME_MARKER not in out
    assert TEXT_MARKER not in out
    assert "Plain summary" not in out and "Synthetic role" not in out


def _requests(out: str) -> int:
    return int(re.search(r"Spend: (\d+) live request", out).group(1))


# --- the refusals --------------------------------------------------------------------------

def test_it_refuses_to_run_without_live(files, monkeypatch, capsys):
    def never():
        raise AssertionError("no judge without --live")

    monkeypatch.setattr(calib, "live_judge", never)
    assert calib.main(files["args"]) == 2
    assert "Refusing to run without --live" in capsys.readouterr().out
    assert not files["cache"].exists()


@pytest.mark.parametrize("cap", ["0", "-1", "nan", "inf", "a dollar"])
def test_it_refuses_a_cap_that_is_no_amount(files, monkeypatch, capsys, cap):
    monkeypatch.setattr(calib, "live_judge", lambda: pytest.fail("no judge on a bad cap"))
    assert calib.main(["--live", "--cap-usd", cap, *files["args"]]) == 2
    assert "Refusing to run" in capsys.readouterr().out
    assert not files["cache"].exists()


def test_it_refuses_a_sample_below_one(files, capsys):
    assert calib.main(["--live", "--sample", "0", *files["args"]]) == 2
    assert "--sample=0" in capsys.readouterr().out


def test_the_default_cap_is_one_dollar_and_reaches_spend_cap(files, monkeypatch):
    seen = {}
    real = jev.SpendCap

    def spy(inner, cap_usd):
        seen["cap"] = cap_usd
        return real(inner, cap_usd)

    monkeypatch.setattr(jev, "SpendCap", spy)
    assert calib.main(["--live", "--sample", "2", *files["args"]]) == 0
    assert seen["cap"] == pytest.approx(1.00)


def test_a_missing_master_or_resume_is_named(files, tmp_path, capsys):
    assert calib.main(["--live", "--master", str(tmp_path / "none.csv"),
                       "--resume", str(files["resume"])]) == 1
    assert calib.main(["--live", "--master", str(files["master"]),
                       "--resume", str(tmp_path / "none.md")]) == 1
    out = capsys.readouterr().out
    assert "No master CSV" in out and "No résumé" in out


# --- a synthetic run -----------------------------------------------------------------------

def test_a_synthetic_run_prints_aggregates_and_no_text(files, capsys):
    assert calib.main(["--live", "--sample", "20", *files["args"]]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "Eligible jobs by Gemini stage 1 score: 1: 8, 2: 8, 3: 8, 4: 8, 5: 8" in out
    assert "Sampled 20 (seed 19): 1: 4, 2: 4, 3: 4, 4: 4, 5: 4" in out
    assert "Job text: 20 from the formatted description, 0 from the job summary" in out
    assert "Stage 1 agreement, 20 jobs (rows Gemini, columns Jev):" in out
    assert "Spearman rank correlation" in out
    assert "At the stage 2 threshold (score 4 or more): agree" in out
    assert "Stage 2 where Gemini ran it: 8 jobs" in out
    assert _requests(out) == 28                       # stage 1 for 20, stage 2 for 8
    _no_text(out + captured.err)
    # the replay cache sits under CACHE_DIR: request hashes and Jev's answers, no text
    cache = files["cache"] / calib.CACHE_FILE
    recorded = json.loads(cache.read_text(encoding="utf-8"))
    assert len(recorded) == 28
    _no_text(cache.read_text(encoding="utf-8"))


def test_a_rerun_reads_the_cache_and_spends_nothing(files, capsys):
    assert calib.main(["--live", "--sample", "10", *files["args"]]) == 0
    first = capsys.readouterr().out
    assert calib.main(["--live", "--sample", "10", *files["args"]]) == 0
    second = capsys.readouterr().out
    assert _requests(first) > 0 and _requests(second) == 0
    assert "0 miss(es)" in second

    def aggregates(out):
        return [line for line in out.splitlines() if not line.startswith("Spend:")]

    assert aggregates(first) == aggregates(second)


def test_the_spend_cap_stops_the_run(files, capsys):
    assert calib.main(["--live", "--cap-usd", "0.0002", "--sample", "20", *files["args"]]) == 3
    out = capsys.readouterr().out
    assert "the spend cap (0.0002 USD) was reached" in out
    assert 1 <= _requests(out) < 20
    assert "Stopped after" in out


def test_a_cap_below_one_request_runs_nothing(files, capsys):
    assert calib.main(["--live", "--cap-usd", "0.000001", *files["args"]]) == 3
    out = capsys.readouterr().out
    assert "Stopped after 0 of" in out and _requests(out) == 0
    assert "nothing to compare" in out


def test_an_outage_stops_the_run(files, monkeypatch, capsys):
    class Gone:
        def judge(self, state, questions):
            raise ConnectionError("service gone")

    monkeypatch.setattr(calib, "live_judge", Gone)
    monkeypatch.setattr(calib, "RETRY_DELAYS_S", ())
    assert calib.main(["--live", "--sample", "5", *files["args"]]) == 3
    captured = capsys.readouterr()
    assert "Jev is unavailable (ConnectionError)" in captured.out
    _no_text(captured.out + captured.err)


def test_an_unavailable_judge_is_named_without_a_key(files, monkeypatch, capsys):
    def unavailable():
        raise jev.JevUnavailable("No TypeSafe API key.")

    monkeypatch.setattr(calib, "live_judge", unavailable)
    assert calib.main(["--live", *files["args"]]) == 1
    assert "Jev is unavailable: No TypeSafe API key." in capsys.readouterr().out


# --- the parts -----------------------------------------------------------------------------

def test_the_rows_left_out(files):
    strata = calib.eligible_ids(files["master"])
    ids = [i for group in strata.values() for i in group]
    assert len(ids) == len(set(ids)) == 40
    assert not any(i.startswith("manual-") or i.startswith("40000" + "9") for i in ids)
    assert "40000000" in strata[1] and "40000000" not in strata[5]


def test_load_jobs_reads_the_text_score_jobs_reads(tmp_path):
    master = _write_master(tmp_path / "m.csv", [
        _row(1, 4), _row(2, 2, job_description_formatted="", job_title="  Two \n Spaces  "),
        _row(3, 3, recommendation="maybe")])
    jobs = calib.load_jobs(master, ["40000002", "40000001", "40000003", "40000009"])
    assert [j.job_id for j in jobs] == ["40000002", "40000001", "40000003"]
    summary, formatted, odd = jobs
    assert (summary.source, summary.title) == ("summary", "Two Spaces")
    assert summary.text == (f"Plain summary {TEXT_MARKER}002. " * 12).strip()
    assert formatted.source == "formatted" and "<li>" not in formatted.text
    assert formatted.text == calib.score_jobs.html_to_md(_desc(1))
    assert (formatted.gemini, formatted.gemini_deep, formatted.gemini_rec) == (4, 8, "apply")
    assert (odd.gemini_deep, odd.gemini_rec) == (None, "")


@pytest.mark.parametrize("extra, got", [
    ({}, (3, "formatted")),
    ({"job_description_formatted": ""}, (3, "summary")),
    ({"score": "4.0"}, (4, "formatted")),
    ({"score": "4.5"}, None),
    ({"score": "6"}, None),
    ({"score": "nan"}, None),
    ({"job_posting_id": " manual-9f "}, None),
    ({"filtered_out": "true "}, None),
    ({"score_reused": "True"}, None),
    ({"reason": "Skills fit good (0.60); domain data science or ML"}, None),
    ({"job_description_formatted": "<p>short</p>", "job_summary": "short"}, None),
])
def test_eligible_rows(extra, got):
    assert calib.eligible(_row(1, 3, **extra)) == got


def test_the_sample_is_stratified_seeded_and_round_robin():
    strata = {1: [f"a{i}" for i in range(100)], 2: [f"b{i}" for i in range(50)],
              3: [f"c{i}" for i in range(5)], 4: [f"d{i}" for i in range(30)],
              5: ["e0", "e1"]}
    picked = calib.stratified_sample(strata, 40, seed=7)
    counts = {s: sum(1 for i in picked if i[0] == "abcde"[s - 1]) for s in strata}
    assert counts == {1: 11, 2: 11, 3: 5, 4: 11, 5: 2}
    assert len(set(picked)) == 40
    assert [i[0] for i in picked[:5]] == list("abcde")      # every score before any repeats
    assert calib.stratified_sample(strata, 40, seed=7) == picked
    assert calib.stratified_sample(strata, 40, seed=8) != picked
    assert sorted(calib.stratified_sample(strata, 1000, seed=7)) == sorted(
        i for group in strata.values() for i in group)
    assert calib.stratified_sample({1: [], 2: []}, 10, seed=7) == []


def test_spearman():
    assert calib.spearman([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert calib.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert calib.spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)
    assert calib.spearman([3, 3, 3], [1, 2, 3]) is None
    assert calib.spearman([1], [1]) is None


def _job(i, gemini, jev_score, deep=None, jev_deep=None, rec="", jev_rec=""):
    job = calib.Job(job_id=f"id{i}", title=f"Title {i}", gemini=gemini, gemini_deep=deep,
                    gemini_rec=rec, source="formatted", text="unused", jev=jev_score)
    if jev_deep is not None:
        job.jev_deep, job.jev_rec, job.stage2 = jev_deep, jev_rec, "compared"
    return job


def test_the_report_matrix_threshold_and_largest_disagreements():
    jobs = [_job(1, 5, 5, 9, 9, "apply", "apply"), _job(2, 1, 5), _job(3, 2, 3),
            _job(4, 4, 2, 8, 3, "apply", "skip"), _job(5, 3, 3), _job(6, 4, 4, 6, 9, "consider",
                                                                     "apply")]
    lines = calib.report(jobs, threshold=4, strata={s: ["x"] for s in calib.SCORES},
                         sampled=6, seed=19, done=6, why="")
    text = "\n".join(lines)
    assert "Gemini 1       0      0      0      0      1" in text
    header = lines[lines.index("Stage 1 agreement, 6 jobs (rows Gemini, columns Jev):") + 1]
    rows = [line for line in lines if line.startswith("Gemini ")]
    assert len(rows) == 5 and {len(line) for line in rows} == {len(header)}   # counts under labels
    assert "Exact 50.0%, within one 66.7%" in text
    assert ("At the stage 2 threshold (score 4 or more): agree 66.7% "
            "(both pass 2, Gemini only 1, Jev only 1, neither 2)") in text
    assert "Stage 2 where Gemini ran it: 3 jobs, 3 compared" in text
    assert "recommendation agrees 33.3% of 3" in text
    start = next(i for i, line in enumerate(lines) if line.startswith("Largest disagreements"))
    worst = [line.split()[0] for line in lines[start + 1:]]
    # the stage 1 gap first (4, 2, 1, 0), then the deep score gap (id6 differs by 3 there)
    assert worst == ["id2", "id4", "id3", "id6"]
    assert lines[-1] == "  id6  Title 6  stage 1 4 and 4; deep 6 and 9"


def test_the_report_keeps_ten_disagreements_and_says_when_there_are_none():
    many = [_job(i, 1, 5) for i in range(15)]
    lines = calib.report(many, threshold=4, strata={}, sampled=15, seed=19, done=15, why="")
    assert "Largest disagreements (10): job id, title, Gemini and Jev" in lines
    same = [_job(i, 3, 3) for i in range(3)]
    lines = calib.report(same, threshold=4, strata={}, sampled=3, seed=19, done=3, why="")
    assert lines[-1] == "No disagreements."


def test_a_judge_error_prints_its_class_and_no_request_text(files, monkeypatch, capsys):
    """A judge error's message can quote the request: only its class is printed."""
    class Leaky:
        def judge(self, state, questions):
            raise RuntimeError(json.dumps(state))

    monkeypatch.setattr(calib, "live_judge", Leaky)
    monkeypatch.setattr(jev_score, "_WARNED", set())    # jev_score names each class once a process
    code = calib.main(["--live", "--sample", "3", *files["args"]])
    captured = capsys.readouterr()
    assert code == 0
    _no_text(captured.out + captured.err)
    assert "RuntimeError" in captured.out
