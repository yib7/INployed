"""UI smoke test (manual; not collected by pytest): builds a synthetic master
+ run_stats in a throwaway LOCALAPPDATA, instantiates the dashboard, and
exercises the tracker, details pane, stats tab, calibration export, applicant
sorting, and the ATS keyword module.

Run:  python tests/smoke_ui.py     (a window flashes briefly — expected)
"""
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="ui_smoke_"))
os.environ["LOCALAPPDATA"] = str(tmp)  # isolate seen.db / logs BEFORE imports

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import pandas as pd  # noqa: E402

root = tmp / "LinkedInJobs"
(root / "morning").mkdir(parents=True)

jobs = pd.DataFrame([
    {
        "job_posting_id": "1001", "url": "https://example.com/1001",
        "job_title": "Data Analyst", "company_name": "AlphaCo",
        "job_location": "Remote, US", "job_summary": "Analyze data with SQL and Python." * 5,
        "job_base_pay_range": "$70,000-$85,000", "job_posted_date": "2026-06-11T08:00:00",
        "job_num_applicants": 50, "job_description_formatted": "<p>SQL, Python, Tableau dashboards</p>",
        "is_easy_apply": "true", "run_label": "morning", "score": 5, "reason": "Strong skills match",
        "deep_score": 8, "strengths": "SQL depth | Dashboarding", "gaps": "No Snowflake",
        "recommendation": "apply", "is_seen": "no", "extracted_date": "2026-06-11",
    },
    {
        "job_posting_id": "1002", "url": "https://example.com/1002",
        "job_title": "Junior Data Scientist", "company_name": "BetaCorp",
        "job_location": "NYC", "job_summary": "Entry-level DS role." * 10,
        "job_base_pay_range": "", "job_posted_date": "2026-06-12T08:00:00",
        "job_num_applicants": 3, "job_description_formatted": "<p>Python, ML</p>",
        "is_easy_apply": "false", "run_label": "morning", "score": 5, "reason": "Great fit",
        "deep_score": 9, "strengths": "ML projects", "gaps": "",
        "recommendation": "apply", "is_seen": "no", "extracted_date": "2026-06-12",
    },
    {
        "job_posting_id": "1003", "url": "https://example.com/1003",
        "job_title": "Sales Rep", "company_name": "GammaInc",
        "job_location": "TX", "job_summary": "Sell things." * 10,
        "job_base_pay_range": "", "job_posted_date": "2026-06-12T09:00:00",
        "job_num_applicants": 12, "job_description_formatted": "<p>Quota sales</p>",
        "is_easy_apply": "false", "run_label": "morning", "score": 2, "reason": "Off-domain",
        "deep_score": "", "strengths": "", "gaps": "", "recommendation": "",
        "is_seen": "no", "extracted_date": "2026-06-12",
    },
])
master = root / "linkedin_jobs_master.csv.gz"
jobs.to_csv(master, index=False, compression="gzip")

pd.DataFrame([
    {"timestamp": "2026-06-11T10:30:00", "input_csv": "a.csv", "rows_in": 120,
     "filtered_out": 60, "llm_scored": 60, "llm_errors": 1, "stage2_done": 9,
     "rescore_attempted": 0, "rescore_scored": 0, "llm_calls": 69,
     "prompt_tokens": 150000, "output_tokens": 9000},
    {"timestamp": "2026-06-12T10:30:00", "input_csv": "b.csv", "rows_in": 90,
     "filtered_out": 40, "llm_scored": 50, "llm_errors": 0, "stage2_done": 7,
     "rescore_attempted": 5, "rescore_scored": 5, "llm_calls": 62,
     "prompt_tokens": 130000, "output_tokens": 8000},
]).to_csv(root / "run_stats.csv", index=False)

import ui  # noqa: E402

ui._load_cfg = lambda: {"gdrive_root": str(root), "min_score": 4, "followup_days": 5}

app = ui.App([master])
assert not app.df.empty and "applicants" in app.df.columns

# Settings + Resume Data tabs exist and are wired into the notebook
assert app.tab_settings is not None, "Settings tab frame missing"
assert app.tab_resume_data is not None, "Resume Data tab frame missing"
assert app.tab_answers is not None, "Apply Answers tab frame missing"
tab_labels = [app.nb.tab(t, "text") for t in app.nb.tabs()]
assert any("Settings" in lbl for lbl in tab_labels), f"no Settings tab: {tab_labels}"
assert any("Resume Data" in lbl for lbl in tab_labels), f"no Resume Data tab: {tab_labels}"
assert any("Apply Answers" in lbl for lbl in tab_labels), f"no Apply Answers tab: {tab_labels}"

# High-Score ordering: same score -> fewest applicants first (1002 before 1001)
high_ids = list(app.tv_high.get_children())
assert high_ids[:2] == ["1002", "1001"], f"applicant sort broken: {high_ids}"
vals = app.tv_high.item("1002", "values")
assert "3" in vals, f"applicants column missing: {vals}"

# Tracker: applied + backdated -> follow-up DUE; resume path -> checkmark
jid = "1001"
app.registry.set_status(jid, "applied", company="AlphaCo", job_title="Data Analyst",
                        url="https://example.com/1001")
app.registry._conn.execute(
    "UPDATE app_status SET applied_date='2026-06-01', status_date='2026-06-01'")
app.registry._conn.commit()
app.registry.record_resume(jid, str(tmp))
app._refresh_tracker()
rows = app.tv_tracker.get_children()
assert list(rows) == [jid], f"tracker rows: {rows}"
tvals = app.tv_tracker.item(jid, "values")
assert tvals[0] == "applied" and tvals[4] == "DUE" and tvals[10] == "✓", tvals

# follow-up flow
app.registry.mark_followed_up([jid])
app._refresh_tracker()
assert app.tv_tracker.item(jid, "values")[4] == "done"

# details pane
app._show_details(jid)
detail_text = app.details.get("1.0", "end")
for needle in ("Data Analyst — AlphaCo", "salary: $70,000-$85,000", "SQL depth", "No Snowflake"):
    assert needle in detail_text, f"missing {needle!r} in details:\n{detail_text}"

# stats tab + calibration (refresh — the registry was written to directly,
# bypassing the reload_data that "Mark applied" performs in real usage)
app._refresh_stats()
assert len(app.tv_stats.get_children()) == 2
assert "2 run(s) logged" in app.lbl_stats_summary.cget("text")
assert "1 labeled" in app.lbl_calibration.cget("text"), app.lbl_calibration.cget("text")
app._export_calibration()
assert (tmp / "linkedin_watcher" / "calibration_labels.csv").exists()

# blocklist round-trip (writes into the sandboxed gdrive root)
ui.append_to_blocklist([master], "GammaInc")
assert "GammaInc" in ui.load_local_blocklist([master])
app.reload_data()
assert "1003" not in set(app.df["job_posting_id"]), "blocklisted company still visible"

# job payload for tailor/prep
payload = app._job_payload("1002")
assert payload and payload["company_name"] == "BetaCorp"

app.root.update()
app.root.destroy()

# ATS module (independent of the UI)
from resume_tailor import ats  # noqa: E402

jd = "We need Python, SQL, Tableau and AWS experience. Python daily. ETL pipelines."
kws = ats.extract_keywords(jd)
assert "Python" in kws or "python" in [k.lower() for k in kws], kws
frac, present, missing = ats.coverage(kws, "Built Python ETL dashboards in Tableau with SQL.")
assert 0 < frac <= 1 and any(k.lower() == "aws" for k in missing), (frac, present, missing)

print("SMOKE TEST OK")
