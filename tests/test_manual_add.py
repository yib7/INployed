"""SP10: the toolkit-agnostic manual-add pipeline (parse -> score -> tailor -> append).

The scorer's Gemini client and the résumé tailor are MOCKED exactly the way the
existing suite mocks them (FakePool mirrors test_score_jobs.py; tailor_fn is a
stand-in), so no real API key is ever needed and no money is spent.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "local")):
    if p not in sys.path:
        sys.path.insert(0, p)

import jobsdata  # noqa: E402
import manual_add  # noqa: E402
import score_jobs as sj  # noqa: E402

_JD = (
    "Data Analyst\n"
    "Acme Corp\n"
    "We are looking for a data analyst to build dashboards in SQL and Python. "
    "You will analyze data, build reports, and communicate findings to stakeholders. "
    "No prior full-time experience required; entry-level welcome.\n"
) * 3


def _resp(text):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
    )


class FakePool:
    """Mirrors test_score_jobs.FakePool — a mocked Gemini client (no network)."""

    def __init__(self, score=5):
        self.score = score
        self.calls = []

    async def generate(self, *, model, contents, config):
        self.calls.append((model, contents))
        if model == sj.STAGE1_MODEL:
            return _resp(json.dumps({"score": self.score, "reason": "great fit"}))
        return _resp(json.dumps(
            {"deep_score": 8, "strengths": ["python", "sql"], "gaps": ["go"],
             "recommendation": "apply"}))

    def stats(self):
        return {"free_calls": len(self.calls), "vertex_calls": 0}


def _fake_tailor_factory(tmp_path):
    seen = {}

    def fake_tailor(job, **kwargs):
        seen["job"] = job
        seen["kwargs"] = kwargs
        out = tmp_path / "Generated" / job.get("company_name", "X")
        out.mkdir(parents=True, exist_ok=True)
        return out

    return fake_tailor, seen


# ── parse / build_job_record ──────────────────────────────────────────────────

def test_build_job_record_from_pasted_jd_marks_source_manual():
    rec = manual_add.build_job_record(jd_text=_JD, url="https://x/1")
    assert rec["source"] == "manual"
    assert manual_add.is_manual_id(rec["job_posting_id"])
    assert rec["job_title"] == "Data Analyst"        # guessed from first line
    assert rec["company_name"] == "Acme Corp"        # guessed from second line
    assert rec["url"] == "https://x/1"
    assert "data analyst" in rec["job_description_formatted"].lower()
    assert rec["run_label"] == "manual"


def test_build_job_record_explicit_fields_win():
    rec = manual_add.build_job_record(
        jd_text=_JD, title="ML Engineer", company="Globex")
    assert rec["job_title"] == "ML Engineer"
    assert rec["company_name"] == "Globex"


def test_build_job_record_rejects_too_short_jd():
    import pytest
    with pytest.raises(ValueError):
        manual_add.build_job_record(jd_text="too short")


def test_manual_id_is_stable_and_dedup_friendly():
    a = manual_add.manual_job_id(_JD, "https://x/1")
    b = manual_add.manual_job_id("different text", "https://x/1")  # url keyed
    c = manual_add.manual_job_id(_JD, "https://x/2")
    assert a == b              # same URL -> same id (re-add de-dupes)
    assert a != c             # different URL -> different id


# ── score_record: drives the REAL two-stage pipeline with a mocked pool ───────

def test_score_record_uses_two_stage_pipeline(monkeypatch):
    rec = manual_add.build_job_record(jd_text=_JD)
    pool = FakePool(score=5)
    out = manual_add.score_record(rec, pool=pool, resume="dummy resume")
    assert out["score"] == 5
    assert out["recommendation"] == "apply"     # stage-2 ran (score >= threshold)
    assert out["deep_score"] == 8
    # both stages of the real scorer were exercised through the mocked pool
    models = {m for m, _ in pool.calls}
    assert sj.STAGE1_MODEL in models and sj.STAGE2_MODEL in models


# ── end-to-end pasted-JD path (scorer + tailor mocked) ────────────────────────

def test_add_manual_job_pasted_jd_end_to_end(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, seen = _fake_tailor_factory(tmp_path)
    res = manual_add.add_manual_job(
        jd_text=_JD, url="https://x/1", pool=FakePool(), resume="r",
        tailor_fn=fake_tailor, master_csv=master,
        tailor_opts={"cover_letter": False, "ats_report": True})

    rec = res["record"]
    assert rec["source"] == "manual" and rec["score"] == 5
    assert res["resume_dir"] is not None and res["appended"] is True
    # the tailor was handed the manual record (the SAME engine scraped jobs use)
    assert seen["job"]["job_posting_id"] == rec["job_posting_id"]

    # a correctly-shaped row landed in the master with source=manual + the resume path
    m = pd.read_csv(master)
    assert len(m) == 1
    row = m.iloc[0]
    assert row["source"] == "manual"
    assert str(row["score"]) == "5"
    assert "data analyst" in str(row["job_description_formatted"]).lower()


def test_add_manual_job_dedupes_on_readd(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, _ = _fake_tailor_factory(tmp_path)
    kw = dict(jd_text=_JD, url="https://x/1", pool=FakePool(), resume="r",
              tailor_fn=fake_tailor, master_csv=master)
    first = manual_add.add_manual_job(**kw)
    second = manual_add.add_manual_job(**kw)
    assert first["appended"] is True
    assert second["appended"] is False                 # same job -> no duplicate row
    assert len(pd.read_csv(master)) == 1


def test_add_manual_job_survives_tailor_failure(tmp_path):
    """A tailor failure must not lose the job — it's still scored + appended."""
    master = tmp_path / "linkedin_jobs_master.csv"

    def boom_tailor(job, **k):
        raise RuntimeError("pdflatex missing")

    res = manual_add.add_manual_job(
        jd_text=_JD, pool=FakePool(), resume="r",
        tailor_fn=boom_tailor, master_csv=master)
    assert res["resume_dir"] is None       # tailoring failed...
    assert res["appended"] is True          # ...but the scored job was still added
    assert pd.read_csv(master).iloc[0]["source"] == "manual"


# ── URL path: fetch mocked, and the pasted-JD fallback when fetch fails ───────

def test_url_path_uses_fetched_text_when_no_paste(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, _ = _fake_tailor_factory(tmp_path)
    fetched = ("Senior nothing\nWidgetCo\n"
               "Build data pipelines in Python and SQL for an entry-level analyst role. "
               "Communicate insights to stakeholders. No experience required.\n") * 3
    res = manual_add.add_manual_job(
        url="https://widgetco/jobs/9", pool=FakePool(), resume="r",
        tailor_fn=fake_tailor, master_csv=master,
        fetch_fn=lambda _u: fetched)            # network mocked
    rec = res["record"]
    assert rec["source"] == "manual"
    assert "data pipelines" in rec["job_description_formatted"].lower()
    assert res["appended"] is True


def test_url_path_falls_back_to_requiring_paste_when_fetch_fails(tmp_path):
    import pytest
    master = tmp_path / "linkedin_jobs_master.csv"
    with pytest.raises(ValueError):           # no paste + empty fetch -> clear error
        manual_add.add_manual_job(
            url="https://blocked/jobs/9", pool=FakePool(), resume="r",
            tailor_fn=lambda *a, **k: tmp_path, master_csv=master,
            fetch_fn=lambda _u: "")           # site blocked the free GET


def test_fetch_url_text_rejects_non_http():
    assert manual_add.fetch_url_text("") == ""
    assert manual_add.fetch_url_text("ftp://x/y") == ""
    assert manual_add.fetch_url_text("not a url") == ""


def test_fetch_url_text_strips_html(monkeypatch):
    html = ("<html><head><style>x{}</style><script>var a=1;</script></head>"
            "<body><h1>Data Analyst</h1><p>" + "Build dashboards in SQL. " * 5
            + "</p></body></html>")

    class _Resp:
        status_code = 200
        text = html

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    out = manual_add.fetch_url_text("https://x/1")
    assert "Data Analyst" in out
    assert "<" not in out and "var a=1" not in out   # tags + script body removed


# ── jobsdata.append_manual_job persistence (schema + dedup + gz bridge) ───────

def test_append_manual_job_writes_master_and_gz(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    rec = {
        "job_posting_id": "manual-abc123", "url": "https://x/1",
        "job_title": "Data Analyst", "company_name": "Acme",
        "job_description_formatted": "<p>full JD here with enough length</p>" * 3,
        "job_summary": "summary", "source": "manual", "run_label": "manual",
        "extracted_date": "2026-06-26", "score": 5, "recommendation": "apply",
        "is_seen": "no",
    }
    added = jobsdata.append_manual_job(rec, master_csv=master)
    assert added is True

    m = pd.read_csv(master, dtype={"job_posting_id": str})
    assert list(m["job_posting_id"]) == ["manual-abc123"]
    assert m.iloc[0]["source"] == "manual"
    assert "job_description_formatted" in m.columns      # JD carried into master

    gz = master.parent / "manual" / "manual_jobs_scored.csv.gz"
    assert gz.exists()
    g = pd.read_csv(gz, dtype={"job_posting_id": str}, compression="gzip")
    assert g.iloc[0]["source"] == "manual"
    assert "job_description_formatted" not in g.columns  # gz drops raw JD (like scored runs)

    # re-append same id -> no duplicate, returns False
    assert jobsdata.append_manual_job(rec, master_csv=master) is False
    assert len(pd.read_csv(master)) == 1


def test_local_run_files_includes_manual(tmp_path):
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    f = manual_dir / "manual_jobs_scored.csv.gz"
    pd.DataFrame([{"job_posting_id": "manual-x"}]).to_csv(
        f, index=False, compression="gzip")
    files = jobsdata.local_run_files(base=tmp_path)
    assert f in files
