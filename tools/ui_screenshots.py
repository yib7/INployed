"""Offscreen UI screenshot harness for the restyle work.

Run:  python tools/ui_screenshots.py [prefix]     (prefix defaults to "current")

Builds the real MainWindow offscreen (never app.main()/win.start() -- no
single-instance lock, no real data loaders), feeds it synthetic data covering
every visual state, and saves one PNG per tab at UI scales 0.75 / 1.0 / 1.5
plus an empty-state pass, into the gitignored `.screenshots/` dir at the repo
root. Idempotent (overwrites), quiet on success -- prints one summary line.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
            "job_summary": f"Synthetic summary for {title} at {company}.",
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


def _write_queue(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "jobs": jobs}, indent=1),
                    encoding="utf-8")


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
    win._apply_df_views()
    _write_queue(queue_path, _queue_jobs())
    win.apply_queue_panel.refresh()
    _select_row0(win)
    app.processEvents()
    written += _capture_all(app, win, prefix, "", SCALES)

    # Note limits of the offscreen capture rather than pretending otherwise.
    print(f"{written} PNG(s) written to {OUT_DIR} (prefix '{prefix}'). "
          "Stats tab shows its placeholder (no run_stats.csv in synthetic mode).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
