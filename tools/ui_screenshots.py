"""Offscreen UI screenshot harness for the restyle work.

Run:  python tools/ui_screenshots.py [prefix]     (prefix defaults to "current")

Builds the real MainWindow offscreen (never app.main()/win.start() -- no
single-instance lock, no real data loaders), feeds it synthetic data covering
every visual state, and saves one PNG per tab at UI scales 0.75 / 1.0 / 1.5
plus an empty-state pass, into the gitignored `.screenshots/` dir at the repo
root. Idempotent (overwrites), quiet on success -- prints one summary line.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform uses the freetype font database, which finds no fonts
# on Windows by default -- every glyph renders as a tofu box. Point it at the
# system font dir so the screenshots show real text.
if os.name == "nt":
    windir = os.environ.get("WINDIR", r"C:\Windows")
    os.environ.setdefault("QT_QPA_FONTDIR", str(Path(windir) / "Fonts"))
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import pandas as pd  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

import apply_queue  # noqa: E402
import resume_md  # noqa: E402
import settings as _settings  # noqa: E402
from resume_tailor import apply_answers as _apply_answers  # noqa: E402
from resume_tailor import config as _rt_config  # noqa: E402
from qt import theme  # noqa: E402
from qt.jobs_tab import JobsTab  # noqa: E402
from qt.main_window import TAB_TITLES, MainWindow  # noqa: E402

OUT_DIR = REPO / ".screenshots"
SCALES = (0.75, 1.0, 1.5)

SLUGS = {
    "High Score (Unseen)": "high_score",
    "All Jobs": "all_jobs",
    "Tracker": "tracker",
    "Auto-apply": "auto_apply",
    "Stats": "stats",
    "Resume Data": "resume_data",
    "Apply Answers": "apply_answers",
    "Settings": "settings",
}

# One row per visual state: reco apply/consider/skip, scores 1-5, deep 0-10,
# one tailored (blue tint), one tailor-failed (red tint), seen + unseen rows.
_JOBS = [
    # jid, score, deep, reco, seen, applicants, title, company
    ("j1", 5, 9.5, "apply",    "no",  3,   "Senior Data Engineer",   "Acme Analytics"),
    ("j2", 5, 8.0, "apply",    "no",  12,  "ML Platform Engineer",   "Globex"),
    ("j3", 4, 6.5, "consider", "no",  57,  "Data Scientist",         "Initech"),
    ("j4", 4, 4.0, "consider", "no",  120, "Analytics Engineer",     "Umbrella Corp"),
    ("j5", 3, 2.5, "consider", "yes", 8,   "BI Developer",           "Hooli"),
    ("j6", 2, 1.0, "skip",     "yes", 0,   "Junior Analyst",         "Pied Piper"),
    ("j7", 1, 0.0, "skip",     "no",  240, "Sales Engineer",         "Vandelay Industries"),
    # Extra unseen high-scorers so the High Score tab reads like a real run.
    ("j8",  5, 9.1, "apply",    "no", 5,  "LLM Application Engineer", "Signalcraft AI"),
    ("j9",  5, 8.6, "apply",    "no", 9,  "Machine Learning Engineer", "Helios Data Labs"),
    ("j10", 5, 8.2, "apply",    "no", 14, "Applied Scientist",        "Vectorly"),
    ("j11", 4, 7.7, "apply",    "no", 18, "Data Platform Engineer",   "Copperleaf Systems"),
    ("j12", 4, 7.3, "apply",    "no", 26, "ML Engineer, Ranking",     "Umbra Analytics"),
    ("j13", 4, 6.9, "consider", "no", 33, "Decision Scientist",       "Aurora Insights"),
    ("j14", 4, 6.4, "consider", "no", 41, "NLP Engineer",             "Larkspur Bio"),
    ("j15", 4, 6.1, "consider", "no", 64, "GenAI Engineer",           "Sundial Commerce"),
    ("j16", 4, 5.8, "consider", "no", 88, "Quantitative Analyst",     "Quanta Metrics"),
]


def _jobs_df() -> pd.DataFrame:
    rows = []
    for i, (jid, score, deep, reco, seen, appl, title, company) in enumerate(_JOBS):
        rows.append({
            "job_posting_id": jid,
            "score": score,
            "deep_score": deep,
            "recommendation": reco,
            "is_seen": seen,
            "applicants": appl,
            "job_num_applicants": appl,
            "extracted_date": f"2026-07-{10 + (i % 2):02d}",
            "run_label": "synthetic",
            "job_title": title,
            "company_name": company,
            "job_location": "Remote, US" if i % 2 else "New York, NY",
            "url": f"https://example.com/jobs/{jid}",
            "job_posted_date": "2026-07-09",
            "is_easy_apply": bool(i % 2),
            "job_base_pay_range": "$105k–$135k" if i % 2 == 0 else "",
            "reason": (f"Synthetic reason: {title} matches the LLM-pipeline "
                       "experience; domain framing is learnable."),
            "strengths": ("Built an LLM draft-validate-stream product|"
                          "Schema-grounded prompting matches their stack|"
                          "Python depth across the listed requirements"),
            "gaps": "No fintech / compliance background",
            "job_summary": f"Synthetic summary for {title} at {company}. " * 4,
            "job_description": f"Synthetic description for {title} at {company}. " * 8,
            "job_description_formatted": f"Synthetic formatted JD for {title}.",
        })
    return pd.DataFrame(rows)


def _status_rows() -> list[dict]:
    """Tracker rows covering every status + follow-up done AND DUE."""
    return [
        # applied long ago, never followed up -> follow_up == DUE
        {"job_posting_id": "j1", "status": "applied", "status_date": "2026-06-01",
         "applied_date": "2026-06-01", "followed_up_at": "",
         "job_title": "Senior Data Engineer", "company": "Acme Analytics",
         "url": "https://example.com/jobs/j1"},
        # applied + followed up -> follow_up == done
        {"job_posting_id": "j2", "status": "applied", "status_date": "2026-06-20",
         "applied_date": "2026-06-20", "followed_up_at": "2026-06-28",
         "job_title": "ML Platform Engineer", "company": "Globex",
         "url": "https://example.com/jobs/j2"},
        {"job_posting_id": "j3", "status": "interviewing", "status_date": "2026-07-01",
         "applied_date": "2026-06-15", "followed_up_at": "",
         "job_title": "Data Scientist", "company": "Initech",
         "url": "https://example.com/jobs/j3"},
        {"job_posting_id": "j4", "status": "offer", "status_date": "2026-07-08",
         "applied_date": "2026-06-10", "followed_up_at": "2026-06-25",
         "job_title": "Analytics Engineer", "company": "Umbrella Corp",
         "url": "https://example.com/jobs/j4"},
        {"job_posting_id": "j5", "status": "rejected", "status_date": "2026-07-05",
         "applied_date": "2026-06-18", "followed_up_at": "",
         "job_title": "BI Developer", "company": "Hooli",
         "url": "https://example.com/jobs/j5"},
    ]


def _queue_jobs() -> list[dict]:
    """One auto-apply queue entry per status."""
    entries = []
    statuses = ("queued", "tailoring", "in_progress", "ready_to_submit",
                "needs_human", "submitted", "failed")
    for i, status in enumerate(statuses):
        jid = f"q{i + 1}"
        # new_entry rejects nothing in STATUSES; "tailoring" etc. all valid.
        e = apply_queue.new_entry(
            jid, company=f"Queue Co {i + 1}", title=f"{status.replace('_', ' ').title()} Role",
            apply_url=f"https://boards.greenhouse.io/queueco{i + 1}/jobs/{jid}",
            status="queued")
        e["status"] = status
        e["attempts"] = i % 3
        e["notes"] = f"synthetic {status} entry"
        if status == "needs_human":
            e["missing_answers"] = ["desired_salary", "notice_period"]
        e["updated_at"] = "2026-07-11T09:00:00"
        entries.append(e)
    return entries


def _write_run_stats(path: Path) -> None:
    """Synthetic run_stats.csv so the Stats tab + freshness chip render populated."""
    cols = ["timestamp", "input_csv", "rows_in", "filtered_out", "llm_scored",
            "llm_errors", "stage2_done", "rescore_attempted", "rescore_scored",
            "llm_calls", "prompt_tokens", "output_tokens", "free_calls", "vertex_calls"]
    now = datetime.now()
    rows = []
    for i in range(6, -1, -1):  # 7 runs, oldest first, newest ~3h ago
        ts = now - timedelta(hours=3 + 12 * i)
        rows.append({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "input_csv": f"synthetic_run_{7 - i}.csv",
            "rows_in": 140 + 9 * i, "filtered_out": 96 + 7 * i,
            "llm_scored": 44 + 2 * i, "llm_errors": 0,
            "stage2_done": 11 + i, "rescore_attempted": 2, "rescore_scored": 2,
            "llm_calls": 46 + 2 * i, "prompt_tokens": 118_000 + 4_000 * i,
            "output_tokens": 9_200 + 300 * i, "free_calls": 30, "vertex_calls": 16 + 2 * i,
        })
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _write_queue(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "jobs": jobs}, indent=1),
                    encoding="utf-8")


def _sanitize_personal_tabs(tmp_dir: Path) -> None:
    """Point the Resume Data + Apply Answers editors at fictional files so no
    real personal data (the user's yaml / answer store) can appear in a grab."""
    example = REPO / "resume_tailor_files" / "master_experience.example.yaml"
    synth_yaml = tmp_dir / "master_experience.yaml"
    synth_yaml.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    _rt_config.MASTER_YAML = synth_yaml
    resume_md.MASTER_YAML_PATH = synth_yaml
    store = tmp_dir / "apply_answers.json"
    answers = _apply_answers.load_with_defaults(path=store)  # defaults only
    fictional = {
        "years_experience": "3", "street_address": "100 Example Street",
        "city": "Springfield", "state": "Illinois", "zip": "00000",
        "country": "United States", "how_heard": "LinkedIn",
    }
    for e in answers:
        if e["id"] in fictional:
            e["answer"] = fictional[e["id"]]
    _apply_answers.save(answers, path=store)
    _apply_answers.STORE_PATH = store
    # Settings tab: read every backing file from the temp dir, never the real
    # .env / config.json (keeps project ids, names, and machine paths out).
    env_path = tmp_dir / ".env"
    env_path.write_text(
        "BRIGHT_DATA_API_TOKEN=synthetic-placeholder-token\n"
        "BRIGHT_DATA_DATASET_ID=gd_exampledataset0001\n"
        "GEMINI_API_KEYS=synthetic-placeholder-key\n"
        "GOOGLE_CLOUD_PROJECT=example-project\n"
        "RESUME_TAILOR_CANDIDATE=Jane_Doe\n"
        "RESUME_TAILOR_OUTPUT=C:/Users/jane/Downloads/Generated_Resumes\n",
        encoding="utf-8")
    for target in list(_settings.TARGET_FILES):
        if target == "env":
            _settings.TARGET_FILES[target] = env_path
        else:
            p = tmp_dir / f"synthetic_{target}.json"
            p.write_text("{}", encoding="utf-8")
            _settings.TARGET_FILES[target] = p


def _registry(resume_dir: Path) -> MagicMock:
    reg = MagicMock()
    reg.resume_paths.return_value = {"j1": str(resume_dir)}  # j1 -> tailored tint + tracker check
    reg.tailor_failure_ids.return_value = {"j2"}             # j2 -> tailor-failed tint
    reg.status_rows.return_value = _status_rows()
    reg.resume_path.return_value = ""                        # Apply button stays disabled
    reg.all_ids.return_value = set()
    return reg


def _select_row0(win: MainWindow) -> None:
    """Select row 0 in every table so detail/preview panes are populated."""
    for tab in (win.high_tab, win.all_tab, win.tracker_tab):
        if isinstance(tab, JobsTab) and tab.table.model().rowCount() > 0:
            tab.table.selectRow(0)
    if win.apply_queue_panel.table.rowCount() > 0:
        win.apply_queue_panel.table.selectRow(0)


def _capture_all(app, win: MainWindow, prefix: str, tag: str, scales) -> int:
    n = 0
    for scale in scales:
        theme.set_scale(app, scale)
        for title in TAB_TITLES:
            win.tabs.setCurrentWidget(win._tab_widgets[title])
            if title == "Tracker":
                # Re-fire the selection WITH the Tracker tab current so the
                # detail card renders its tracker variant (status/follow-up
                # pills + NEXT STEP), not the discovery one it got when the
                # row was first selected under another tab.
                table = win.tracker_tab.table
                if table.model().rowCount() > 0:
                    table.clearSelection()
                    table.selectRow(0)
            win.resize(1600, 1000)
            app.processEvents()
            out = OUT_DIR / f"{prefix}_{tag}{SLUGS[title]}_{scale}.png"
            if not win.grab().save(str(out)):
                print(f"NOTE: could not save {out}")
                continue
            n += 1
    return n


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "current"
    OUT_DIR.mkdir(exist_ok=True)

    tmp = tempfile.TemporaryDirectory(prefix="ui_screenshots_")
    tmp_dir = Path(tmp.name)
    queue_path = tmp_dir / "apply_queue.json"
    _write_queue(queue_path, [])  # start empty for the empty-state pass
    # Point the panel at the synthetic queue BEFORE the window constructs
    # (path is resolved at call time; this also keeps the real queue untouched).
    os.environ["APPLY_QUEUE_PATH"] = str(queue_path)
    resume_dir = tmp_dir / "resume_j1"
    resume_dir.mkdir()
    _sanitize_personal_tabs(tmp_dir)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    theme.apply_theme(app)
    win = MainWindow(csv_paths=[], registry=_registry(resume_dir))
    win.show()
    app.processEvents()

    # Pass 1: empty states (no jobs df, empty queue, mock tracker rows already
    # present via the registry -- cheap and harmless). Scale 1.0 only.
    written = _capture_all(app, win, prefix, "empty_", (1.0,))

    # Pass 2: populated. Install the synthetic frame through the same path the
    # app uses so high/all/tracker/stats all refresh consistently.
    win.min_score = 4
    win.df = _jobs_df()
    # Point the Stats tab at a synthetic run_stats.csv (never the user's real
    # Drive folder -- pin the root resolver to the temp dir) so the metrics
    # table and the "Fresh" chip render instead of the not-synced placeholder.
    _write_run_stats(tmp_dir / "run_stats.csv")
    from qt import main_window as _mw  # noqa: E402
    _mw.gdrive_root_dir = lambda _paths: tmp_dir
    win._apply_df_views()
    win._refresh_stats()
    _write_queue(queue_path, _queue_jobs())
    win.apply_queue_panel.refresh()
    _select_row0(win)
    app.processEvents()
    written += _capture_all(app, win, prefix, "", SCALES)

    print(f"{written} PNG(s) written to {OUT_DIR} (prefix '{prefix}').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
