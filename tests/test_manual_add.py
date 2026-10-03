"""SP10/SP5: the toolkit-agnostic manual-add pipeline (parse -> tailor -> append).

The user already chose this job (SP5/MA-1), so there is no scoring step at all;
`test_add_manual_job_never_calls_scorer` pins that. The résumé tailor is MOCKED
exactly the way the existing suite mocks it (tailor_fn is a stand-in), so no
real API key is ever needed and no money is spent.
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO / "pipeline"), str(REPO / "local")):
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


# ── MA-1: no scoring step at all -- save and tailor at once ───────────────────

def test_add_manual_job_never_calls_scorer(monkeypatch, tmp_path):
    """The user already chose this job: add_manual_job must never touch the
    scorer, in any form (this is the RED/GREEN pin for SP5/MA-1)."""
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, _ = _fake_tailor_factory(tmp_path)

    def boom(*a, **k):
        raise AssertionError("add_manual_job must never call the scorer")

    monkeypatch.setattr(sj, "run_scoring", boom)
    monkeypatch.setattr(sj, "make_pool", boom)
    res = manual_add.add_manual_job(
        jd_text=_JD, url="https://x/1", tailor_fn=fake_tailor, master_csv=master)
    assert res["appended"] is True
    assert "score" not in res["record"]


def test_add_manual_job_signature_drops_scoring_params():
    import inspect
    params = set(inspect.signature(manual_add.add_manual_job).parameters)
    assert not params & {"pool", "pool_factory", "resume", "do_tailor"}


def test_score_record_was_removed():
    assert not hasattr(manual_add, "score_record")


# ── end-to-end pasted-JD path (tailor mocked, no scoring) ─────────────────────

def test_add_manual_job_pasted_jd_end_to_end(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, seen = _fake_tailor_factory(tmp_path)
    res = manual_add.add_manual_job(
        jd_text=_JD, url="https://x/1", tailor_fn=fake_tailor, master_csv=master,
        tailor_opts={"cover_letter": False, "ats_report": True})

    rec = res["record"]
    assert rec["source"] == "manual"
    assert res["resume_dir"] is not None and res["appended"] is True
    # the tailor was handed the manual record (the SAME engine scraped jobs use)
    assert seen["job"]["job_posting_id"] == rec["job_posting_id"]

    # a correctly-shaped row landed in the master with source=manual + the resume path
    m = pd.read_csv(master)
    assert len(m) == 1
    row = m.iloc[0]
    assert row["source"] == "manual"
    assert "data analyst" in str(row["job_description_formatted"]).lower()


def test_add_manual_job_dedupes_on_readd(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, _ = _fake_tailor_factory(tmp_path)
    kw = dict(jd_text=_JD, url="https://x/1", tailor_fn=fake_tailor, master_csv=master)
    first = manual_add.add_manual_job(**kw)
    second = manual_add.add_manual_job(**kw)
    assert first["appended"] is True
    assert second["appended"] is False                 # same job -> no duplicate row
    assert len(pd.read_csv(master)) == 1


def test_add_manual_job_survives_tailor_failure(tmp_path):
    """A tailor failure must not lose the job; it's still saved (MA-4)."""
    master = tmp_path / "linkedin_jobs_master.csv"

    def boom_tailor(job, **k):
        raise RuntimeError("pdflatex missing")

    res = manual_add.add_manual_job(
        jd_text=_JD, tailor_fn=boom_tailor, master_csv=master)
    assert res["resume_dir"] is None       # tailoring failed...
    assert res["appended"] is True          # ...but the job was still added
    assert pd.read_csv(master).iloc[0]["source"] == "manual"


# ── MA-2: duplicate detection before any spend, and "Tailor again" ────────────

def test_find_duplicate_matches_from_dataframe():
    jid = manual_add.manual_job_id(_JD, "https://x/1")
    df = pd.DataFrame([{"job_posting_id": jid, "job_title": "Data Analyst",
                        "company_name": "Acme Corp", "extracted_date": "2026-09-01"}])
    dup = manual_add.find_duplicate(_JD, "https://x/1", df=df)
    assert dup is not None
    assert dup["job_title"] == "Data Analyst" and dup["company_name"] == "Acme Corp"


def test_find_duplicate_matches_from_master_csv_when_not_in_dataframe(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    _seed(master, manual_add.manual_job_id(_JD, "https://x/1"),
          job_title="Data Analyst", company_name="Acme Corp")
    dup = manual_add.find_duplicate(_JD, "https://x/1", df=None, master_csv=master)
    assert dup is not None and dup["job_title"] == "Data Analyst"


def test_find_duplicate_returns_none_for_new_job(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    df = pd.DataFrame([{"job_posting_id": "manual-other", "job_title": "X"}])
    assert manual_add.find_duplicate(_JD, "https://x/1", df=df, master_csv=master) is None


def test_duplicate_message_includes_date_title_company():
    msg = manual_add.duplicate_message(
        {"job_title": "Data Analyst", "company_name": "Acme Corp",
         "extracted_date": "2026-09-01"})
    assert msg == "Already added on 2026-09-01 as Data Analyst at Acme Corp."


def test_duplicate_message_omits_date_when_missing():
    msg = manual_add.duplicate_message(
        {"job_title": "Data Analyst", "company_name": "Acme Corp"})
    assert msg == "Already added as Data Analyst at Acme Corp."
    assert "on " not in msg


def test_retailor_existing_reuses_existing_row_never_appends(tmp_path):
    fake_tailor, seen = _fake_tailor_factory(tmp_path)
    record = {"job_posting_id": "manual-abc", "job_title": "Data Analyst",
             "company_name": "Acme Corp", "job_description_formatted": _JD}
    res = manual_add.retailor_existing(record, tailor_fn=fake_tailor)
    assert res["appended"] is False
    assert res["resume_dir"] is not None
    assert seen["job"]["job_posting_id"] == "manual-abc"


def test_retailor_existing_survives_tailor_failure():
    def boom_tailor(job, **k):
        raise RuntimeError("pdflatex missing")

    record = {"job_posting_id": "manual-abc", "job_title": "T", "company_name": "C"}
    res = manual_add.retailor_existing(record, tailor_fn=boom_tailor)
    assert res["appended"] is False
    assert res["resume_dir"] is None
    assert res["record"]["job_posting_id"] == "manual-abc"     # row identity preserved


# ── Follow-up 1: a re-pasted JD backfills a prune-blanked stored description ──

def test_retailor_existing_fills_blank_description_from_pasted_jd_text(tmp_path):
    """pipeline/prune_master.py blanks job_description_formatted on a row past
    its retention window. If the user re-pastes the full text into "Tailor
    again", the tailor must see it even though the saved row itself is blank --
    and it must match a FRESH add byte for byte, not the job_summary's
    whitespace-collapsed variant (_strip_html would flatten _JD's embedded
    newlines onto one line, which is not what build_job_record stores)."""
    fake_tailor, seen = _fake_tailor_factory(tmp_path)
    record = {"job_posting_id": "manual-abc", "job_title": "Data Analyst",
             "company_name": "Acme Corp", "job_description_formatted": ""}
    manual_add.retailor_existing(record, jd_text=_JD, tailor_fn=fake_tailor)
    fresh = manual_add.build_job_record(jd_text=_JD, url="https://x/1")
    assert seen["job"]["job_description_formatted"] == fresh["job_description_formatted"]
    assert "\n" in seen["job"]["job_description_formatted"]    # not flattened


def test_retailor_existing_keeps_stored_description_over_pasted_jd_text(tmp_path):
    """A stored description that survived the prune (or was never blanked)
    wins; a re-paste is a fallback for the blank case only, never an override."""
    fake_tailor, seen = _fake_tailor_factory(tmp_path)
    stored = "The original description that was never pruned stays exactly as saved."
    record = {"job_posting_id": "manual-abc", "job_title": "Data Analyst",
             "company_name": "Acme Corp", "job_description_formatted": stored}
    manual_add.retailor_existing(record, jd_text=_JD, tailor_fn=fake_tailor)
    assert seen["job"]["job_description_formatted"] == stored


def test_retailor_existing_backfill_never_writes_the_master_row(tmp_path, monkeypatch):
    """Filling a blank description from the re-paste is for this tailor run
    only: it must never rewrite the saved master row, and the caller's own
    record dict must come back unchanged."""
    fake_tailor, _ = _fake_tailor_factory(tmp_path)

    def boom(*a, **k):
        raise AssertionError("retailor_existing must never write the master")

    monkeypatch.setattr(jobsdata, "append_manual_job", boom)
    monkeypatch.setattr(jobsdata, "update_manual_job", boom)
    record = {"job_posting_id": "manual-abc", "job_title": "Data Analyst",
             "company_name": "Acme Corp", "job_description_formatted": ""}
    res = manual_add.retailor_existing(record, jd_text=_JD, tailor_fn=fake_tailor)
    assert res["record"]["job_description_formatted"]        # filled for the caller
    assert record["job_description_formatted"] == ""         # caller's own dict untouched


# ── URL path: fetch mocked, and the pasted-JD fallback when fetch fails ───────

def test_url_path_uses_fetched_text_when_no_paste(tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    fake_tailor, _ = _fake_tailor_factory(tmp_path)
    fetched = ("Senior nothing\nWidgetCo\n"
               "Build data pipelines in Python and SQL for an entry-level analyst role. "
               "Communicate insights to stakeholders. No experience required.\n") * 3
    res = manual_add.add_manual_job(
        url="https://widgetco/jobs/9", tailor_fn=fake_tailor, master_csv=master,
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
            url="https://blocked/jobs/9",
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
    # This test exercises HTML stripping, not host policy — allow the stub host
    # (P2-16 otherwise blocks unresolvable hosts, fail-closed).
    monkeypatch.setattr(manual_add, "_host_is_private", lambda h: False)
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


# ── delete / update / master_row + removed-jobs filter (item 10) ──────────────

def _seed(master, jid, **over):
    rec = {"job_posting_id": jid, "url": f"https://x/{jid}", "job_title": "T",
           "company_name": "C", "source": "manual", "score": 5,
           "job_description_formatted": "full JD text with enough length here " * 3}
    rec.update(over)
    jobsdata.append_manual_job(rec, master_csv=master)


def test_delete_jobs_removes_everywhere_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)   # isolate config.json
    master = tmp_path / "linkedin_jobs_master.csv"
    _seed(master, "manual-del1")
    _seed(master, "manual-keep2")
    n = jobsdata.delete_jobs(["manual-del1"], master_csv=master)
    assert n == 1
    ids = set(pd.read_csv(master, dtype={"job_posting_id": str})["job_posting_id"])
    assert ids == {"manual-keep2"}                    # dropped from the master
    assert jobsdata.load_removed_jobs() == {"manual-del1"}   # remembered as removed
    gz = master.parent / "manual" / "manual_jobs_scored.csv.gz"
    gids = set(pd.read_csv(gz, dtype={"job_posting_id": str},
                           compression="gzip")["job_posting_id"])
    assert gids == {"manual-keep2"}                   # and from the gz bridge


def test_delete_jobs_marks_removed_before_rewriting_csvs(tmp_path, monkeypatch):
    # Regression: the removed_jobs hide-marker must be written BEFORE the (slow)
    # CSV rewrite, so a reload racing the background delete filters the row out
    # instead of resurrecting it. Assert the ordering invariant directly: at the
    # moment the CSV drop runs, the id is already in removed_jobs.
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)   # isolate config.json
    master = tmp_path / "linkedin_jobs_master.csv"
    _seed(master, "manual-race1")
    seen_when_dropping = {}
    real_drop = jobsdata._drop_ids_from_csv

    def spy(path, ids):
        seen_when_dropping["removed"] = set(jobsdata.load_removed_jobs())
        return real_drop(path, ids)

    monkeypatch.setattr(jobsdata, "_drop_ids_from_csv", spy)
    jobsdata.delete_jobs(["manual-race1"], master_csv=master)
    assert "manual-race1" in seen_when_dropping["removed"]  # marker set first


def test_load_files_hides_removed_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    f = tmp_path / "manual" / "manual_jobs_scored.csv.gz"
    f.parent.mkdir(parents=True)
    pd.DataFrame([{"job_posting_id": "manual-a", "job_title": "A"},
                  {"job_posting_id": "manual-b", "job_title": "B"}]).to_csv(
        f, index=False, compression="gzip")
    jobsdata._save_removed_jobs({"manual-a"})
    df, _ = jobsdata.load_files([f])
    assert set(df["job_posting_id"]) == {"manual-b"}  # removed id filtered out at load


def test_update_manual_job_replaces_row_keeps_id(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    master = tmp_path / "linkedin_jobs_master.csv"
    _seed(master, "manual-e", url="https://old", job_title="Old", company_name="OldCo")
    rec = {"job_posting_id": "manual-e", "url": "https://new", "job_title": "New",
           "company_name": "NewCo", "source": "manual", "score": 5,
           "job_description_formatted": "full JD text with enough length here " * 3}
    jobsdata.update_manual_job(rec, old_id="manual-e", master_csv=master)
    m = pd.read_csv(master, dtype={"job_posting_id": str})
    assert list(m["job_posting_id"]) == ["manual-e"]  # one row, id stable
    assert m.iloc[0]["job_title"] == "New" and m.iloc[0]["company_name"] == "NewCo"
    assert m.iloc[0]["url"] == "https://new"


def test_update_manual_job_unremoves_previously_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    master = tmp_path / "linkedin_jobs_master.csv"
    jobsdata._save_removed_jobs({"manual-e"})
    jobsdata.update_manual_job(
        {"job_posting_id": "manual-e", "job_title": "Re", "company_name": "C",
         "source": "manual", "job_description_formatted": "JD text long enough here " * 3},
        old_id="manual-e", master_csv=master)
    assert "manual-e" not in jobsdata.load_removed_jobs()   # editing resurrects it


def test_master_row_returns_full_row_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    master = tmp_path / "linkedin_jobs_master.csv"
    _seed(master, "manual-m", job_title="DA", company_name="Acme")
    row = jobsdata.master_row("manual-m", master_csv=master)
    assert row and row["job_title"] == "DA" and row["company_name"] == "Acme"
    assert "full jd text" in str(row["job_description_formatted"]).lower()
    assert jobsdata.master_row("nope", master_csv=master) is None


def _big_master(path, n, jd="JD text " * 10):
    pd.DataFrame({
        "job_posting_id": [f"job-{i}" for i in range(n)],
        "job_title": [f"Title {i}" for i in range(n)],
        "company_name": [f"Co {i}" for i in range(n)],
        "score": [None if i % 2 else i for i in range(n)],  # NaN half the rows
        "job_description_formatted": [f"{jd}{i}" for i in range(n)],
    }).to_csv(path, index=False)


def test_master_row_finds_id_deep_in_multichunk_file(tmp_path, monkeypatch):
    # UI-thread safety (audit P1): master_row must stream in bounded chunks, not
    # read the whole master. A row deep in a multi-chunk file must still be found
    # with the same shape as before (all columns, NaN -> "").
    monkeypatch.setattr(jobsdata, "_MASTER_ROW_CHUNK", 10)
    master = tmp_path / "linkedin_jobs_master.csv"
    _big_master(master, 55)
    row = jobsdata.master_row("job-53", master_csv=master)   # chunk 6 of 6
    assert row["job_title"] == "Title 53" and row["company_name"] == "Co 53"
    assert row["score"] == ""                                # NaN -> "" preserved
    assert row["job_description_formatted"].endswith("53")
    assert jobsdata.master_row("job-999", master_csv=master) is None


def test_master_row_reads_chunked_and_stops_at_first_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "_MASTER_ROW_CHUNK", 10)
    master = tmp_path / "linkedin_jobs_master.csv"
    _big_master(master, 55)
    chunks_read = []
    real_read_csv = jobsdata.pd.read_csv

    def spy(*a, **kw):
        assert kw.get("chunksize"), "master_row must pass chunksize (bounded read)"
        reader = real_read_csv(*a, **kw)
        return (chunks_read.append(1) or c for c in reader)

    monkeypatch.setattr(jobsdata.pd, "read_csv", spy)
    row = jobsdata.master_row("job-3", master_csv=master)    # lives in chunk 1
    assert row and row["job_title"] == "Title 3"
    assert len(chunks_read) == 1                             # stopped after first hit


# ── SSRF hardening (audit P2-16) + dead-branch cleanup (P2-3) ─────────────────

def test_fetch_url_text_rejects_private_and_metadata_hosts(monkeypatch):
    """A pasted "job link" pointing at localhost / RFC1918 / the cloud metadata
    endpoint must be refused BEFORE any request is sent."""
    import requests

    called = []
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: called.append(a) or None)
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:8080/secret",
                "http://localhost/admin",
                "http://10.0.0.5/x", "http://192.168.1.1/x"):
        assert manual_add.fetch_url_text(url) == ""
    assert called == []            # refused BEFORE any request went out


def test_fetch_url_text_rejects_redirect_to_private_host(monkeypatch):
    import requests

    class _Redir:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/loot"}
        text = ""

    monkeypatch.setattr(manual_add, "_host_is_private", lambda h: h != "x")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Redir())
    assert manual_add.fetch_url_text("https://x/1") == ""


def test_fetch_url_text_caps_response_size(monkeypatch):
    import requests

    class _Big:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size=8192, decode_unicode=False):
            for _ in range(4):     # 4 x 4MB > the 5MB cap
                yield "A" * (4 * 1024 * 1024)

    monkeypatch.setattr(manual_add, "_host_is_private", lambda h: False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Big())
    out = manual_add.fetch_url_text("https://x/1")
    # never buffers unbounded: whatever comes back is at most the cap
    assert len(out) <= manual_add._MAX_FETCH_BYTES


def test_guess_title_company_uses_raw_lines():
    # P2-3: the old first branch split _strip_html output on newlines that
    # _strip_html had already collapsed — dead code. The raw-line path is the
    # real behavior and must keep working.
    title, company = manual_add._guess_title_company(
        "Data Analyst\nAcme Corp\nBuild dashboards.")
    assert title == "Data Analyst" and company == "Acme Corp"


def test_a_tailor_failure_status_line_names_no_path():
    """The status line is on screen: the exception's absolute path is dropped
    to its file name, and a long multi-line reason shows its first line."""
    def boom_tailor(job, **k):
        raise OSError(r"could not write C:\Users\someone\Generated\resume.pdf" "\n"
                      "Traceback detail line")

    lines = []
    record = {"job_posting_id": "manual-abc", "job_title": "T", "company_name": "C"}
    manual_add.retailor_existing(record, tailor_fn=boom_tailor, on_status=lines.append)
    failed = [m for m in lines if m.startswith("tailoring failed")]
    assert failed == ["tailoring failed (could not write resume.pdf); the job is still "
                      "saved. Retry with Tailor résumé."]
