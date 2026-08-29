"""Offscreen UI screenshot harness for the dashboard.

Run:  python scripts/ui_screenshots.py [prefix]     (prefix defaults to "current")

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

# Keep the maintainer's real `.env` out of this process, BEFORE anything under
# `local/` is imported. `resume_tailor.config` calls `load_dotenv()` at import
# scope and does not honour INPLOYED_NO_DOTENV, so its values would land in
# `os.environ` -- and `output.candidate_slug()` reads RESUME_TAILOR_CANDIDATE
# from there, which would put the real candidate name on the two file paths the
# Apply panel prints in the walkthrough. Patching the attribute (rather than
# setting the flag alone) is what actually works: `from dotenv import load_dotenv`
# inside that module resolves against this object. Both fixture values are then
# pinned, so a maintainer who exports either one in their shell still gets Jane.
os.environ["INPLOYED_NO_DOTENV"] = "1"
try:
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
except ImportError:
    pass
os.environ["RESUME_TAILOR_CANDIDATE"] = "Jane_Doe"
os.environ["RESUME_TAILOR_OUTPUT"] = r"C:\Users\jane\Downloads\Generated_Resumes"

sys.path.insert(0, str(REPO / "local"))

import pandas as pd  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

import apply_queue  # noqa: E402
import jobsdata  # noqa: E402
import resume_md  # noqa: E402
import settings as _settings  # noqa: E402
from resume_tailor import apply_answers as _apply_answers  # noqa: E402
from resume_tailor import config as _rt_config  # noqa: E402
from qt import theme  # noqa: E402
from qt.jobs_tab import JobsTab  # noqa: E402
from qt.main_window import TAB_TITLES, MainWindow  # noqa: E402
from qt.settings_tab import SECTION_ORDER  # noqa: E402

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
            # Two discovery days, yesterday and the day before, so the High Score
            # tab shows its newest-day-first-then-score ranking. Relative for the
            # same reason the tracker's dates are (see `_status_rows`): a README
            # still that says "Found 2026-07-11" reads as abandoned a year on.
            "extracted_date": _ago(2 - (i % 2)),
            "run_label": "synthetic",
            "job_title": title,
            "company_name": company,
            "job_location": "Remote, US" if i % 2 else "New York, NY",
            "url": f"https://example.com/jobs/{jid}",
            "job_posted_date": _ago(3),
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


def _ago(days: int) -> str:
    """An ISO date `days` before today."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _status_rows() -> list[dict]:
    """Tracker rows covering every status + follow-up done AND DUE.

    Dated in days-before-today rather than as literals. The Tracker's Days column
    and its NEXT STEP card both count from `applied_date` to now, so a fixed date
    made those numbers a function of when the media was shot: the same fixture
    read "69 days" in August and "88 days" three weeks later, silently rotting the
    README alt text that quotes it. Offsets keep the frame saying the same thing
    at every re-shoot.
    """
    return [
        # applied long ago, never followed up -> follow_up == DUE
        {"job_posting_id": "j1", "status": "applied", "status_date": _ago(88),
         "applied_date": _ago(88), "followed_up_at": "",
         "job_title": "Senior Data Engineer", "company": "Acme Analytics",
         "url": "https://example.com/jobs/j1"},
        # applied + followed up -> follow_up == done
        {"job_posting_id": "j2", "status": "applied", "status_date": _ago(69),
         "applied_date": _ago(69), "followed_up_at": _ago(61),
         "job_title": "ML Platform Engineer", "company": "Globex",
         "url": "https://example.com/jobs/j2"},
        {"job_posting_id": "j3", "status": "interviewing", "status_date": _ago(58),
         "applied_date": _ago(74), "followed_up_at": "",
         "job_title": "Data Scientist", "company": "Initech",
         "url": "https://example.com/jobs/j3"},
        {"job_posting_id": "j4", "status": "offer", "status_date": _ago(51),
         "applied_date": _ago(79), "followed_up_at": _ago(64),
         "job_title": "Analytics Engineer", "company": "Umbrella Corp",
         "url": "https://example.com/jobs/j4"},
        {"job_posting_id": "j5", "status": "rejected", "status_date": _ago(54),
         "applied_date": _ago(71), "followed_up_at": "",
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
        # Relative, like every other fixture date: a queue whose rows are all
        # stamped with one long-past day reads as an abandoned run.
        e["updated_at"] = (datetime.now() - timedelta(hours=2 + i)
                           ).strftime("%Y-%m-%dT%H:%M:%S")
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


# Dashboard preferences the grab must not inherit from whoever runs it, with the
# value each one is pinned to. `settings_collapsed` folds every Settings section
# (the harness then unfolds exactly one, see `_expand_settings_section`), which is
# what keeps Credentials and its masked secret rows off a README image whatever
# the author last clicked. `ui_scale_pct` has to agree with the scale the frame is
# rendered at, or the "Interface size" readout in the action bar states a number
# the pixels above it contradict.
SYNTHETIC_CONFIG = {
    "settings_collapsed": list(SECTION_ORDER),
    "settings_show_advanced": False,
    "ui_scale_pct": 100,
    "hidden_columns": {},
    "removed_jobs": [],
    "min_score": 4,
    "followup_days": 5,
    "gdrive_root": "",
}


def _pin_dashboard_config(tmp_dir: Path) -> Path:
    """Redirect `local/config.json` to a synthetic copy for the duration of a run.

    Everything in `SYNTHETIC_CONFIG` is a per-user preference read straight out of
    that file, so without this the committed media records whoever ran the script:
    the sections they left unfolded, the columns they hid, the interface scale they
    set. It leaked twice before this existed -- an expanded Credentials card with
    masked key rows on screen, and a 120% readout under a frame drawn at 100%.
    Pinning also means no `save_*` helper fired by the tour can reach the real file.
    """
    cfg = tmp_dir / "config.json"
    cfg.write_text(json.dumps(SYNTHETIC_CONFIG, indent=1), encoding="utf-8")
    jobsdata._cfg_path = lambda: cfg
    return cfg


def _sanitize_personal_tabs(tmp_dir: Path) -> None:
    """Point the Resume Data + Apply Answers editors at fictional files so no
    real personal data (the user's yaml / answer store) can appear in a grab."""
    _pin_dashboard_config(tmp_dir)
    example = REPO / "resume_tailor_files" / "master_experience.example.yaml"
    synth_yaml = tmp_dir / "master_experience.yaml"
    synth_yaml.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    _rt_config.MASTER_YAML = synth_yaml
    resume_md.MASTER_YAML_PATH = synth_yaml
    store = tmp_dir / "apply_answers.json"
    answers = _apply_answers.load_with_defaults(path=store)  # defaults only
    # Keys must be the answer-store ids from `apply_answers` defaults, not the
    # field labels: an id that matches nothing silently leaves the real default in
    # place, which is how five of these went unset (and the apply sheet's Address
    # block rendered with only a country) until 8B caught it in a frame.
    fictional = {
        "years_experience": "3", "address_street": "100 Example Street",
        "address_city": "Springfield", "address_state": "IL", "address_zip": "00000",
        "address_country": "United States", "how_did_you_hear": "LinkedIn",
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


# The job the tour tailors: j1, the row the registry already tints "Tailored".
TAILORED_JOB = {"job_posting_id": "j1", "company_name": "Acme Analytics",
                "job_title": "Senior Data Engineer",
                "url": "https://example.com/jobs/j1"}

# Where the panel SAYS the tailored folder is. The real one is a temp dir whose
# path carries the OS account name of whoever runs this, and that path is on
# screen in two copyable fields, so the frame shows the synthetic output root the
# rest of the fixtures use (`RESUME_TAILOR_OUTPUT` in `_sanitize_personal_tabs`)
# instead. Cosmetic, and the only string in the media that is not what the code
# produced.
SHOWN_OUTPUT_ROOT = r"C:\Users\jane\Downloads\Generated_Resumes"

# One re-phrased line per atom in `master_experience.example.yaml`, keyed the way
# the tailor keys them (the atom ids joined by "+"). Written by hand here for the
# same reason the résumé engine writes them at all: every one traces to a `what` /
# `how` / `scope` / `impact` field in that file, and nothing is invented.
_DEMO_BULLETS = {
    "pipeline_speedup": (
        "Rebuilt a nightly ingestion pipeline over ~2M records/day from 12 source "
        "systems, replacing sequential REST calls with a batched async fetcher: "
        "runtime 6h to 90min and about $400/mo less cloud spend."),
    "test_coverage": (
        "Raised billing-service test coverage from 42% to 81% with pytest suites "
        "and CI gates, catching 3 regressions before release."),
    "core_feature": (
        "Built a web app that turns plain English into runnable queries, with an "
        "LLM drafting the query, a validator checking it and results streaming "
        "back, so non-technical users never write SQL."),
    "membership_growth": (
        "Ran weekly workshops as club president and grew active membership from "
        "20 to 60 over one academic year."),
}

_DEMO_SELECTION = {
    "experience": [{"name": "Example Corp",
                    "groups": [["pipeline_speedup"], ["test_coverage"]]}],
    "projects": [{"name": "ExampleApp", "groups": [["core_feature"]]}],
    "leadership": [{"name": "Campus Coding Club", "groups": [["membership_growth"]]}],
}

_DEMO_SKILL_LINES = [
    {"label": "Languages", "items": "Python, JavaScript, SQL, Java"},
    {"label": "Frameworks", "items": "React, FastAPI, Spark"},
    {"label": "Developer tools", "items": "Git, Docker, AWS, GitHub Actions, PostgreSQL"},
    {"label": "Libraries", "items": "pandas, NumPy, PyTorch, scikit-learn"},
]

_DEMO_COVER = (
    "Dear Hiring Team,\n\n"
    "I am applying for the Senior Data Engineer role at Acme Analytics. Rebuilding "
    "a nightly ingestion pipeline over ~2M records a day, and cutting its runtime "
    "from six hours to ninety minutes, is the closest thing I have to the batch "
    "work your posting describes.\n\n"
    "Thank you for your time.\n\nJane Doe\n"
)


def _tailored_folder(tmp_dir: Path) -> Path:
    """A folder that looks exactly like a finished tailor run, produced without one.

    `apply_data.write` is the real writer, reading the real synthetic master and
    answer store, so the apply sheet on screen is the shipped format rather than a
    mock-up of it. What a real run would add on top is an LLM call that picks the
    atoms and re-phrases them; both are supplied here as fixtures, which is why
    this costs nothing and puts no personal data on screen. The two PDFs are
    placeholders: `build_apply_context` only checks that the résumé one exists.
    """
    from resume_tailor import apply_data, output
    folder = tmp_dir / "tailored_j1"
    folder.mkdir(exist_ok=True)
    (folder / output.resume_filename()).write_bytes(b"%PDF-1.4 synthetic\n")
    (folder / output.cover_filename()).write_bytes(b"%PDF-1.4 synthetic\n")
    apply_data.write(TAILORED_JOB, folder, sel=_DEMO_SELECTION, bullets=_DEMO_BULLETS,
                     skill_lines=_DEMO_SKILL_LINES, cover_body=_DEMO_COVER)
    return folder


def _show_apply_panel(win: MainWindow, folder: Path) -> None:
    """Open the right-side Apply panel on `folder`, the way a finished Tailor does.

    Goes through `apply.build_apply_context`, so the sheet is parsed and rendered
    by shipped code; only the two path strings the panel prints are swapped for
    `SHOWN_OUTPUT_ROOT` before it sees them. `_finish_apply_ok` is deliberately not
    used: it would put a path on the machine's real clipboard.
    """
    from resume_tailor import apply as apply_mod
    ctx = apply_mod.build_apply_context(folder)
    shown = Path(SHOWN_OUTPUT_ROOT) / TAILORED_JOB["company_name"] / TAILORED_JOB["job_title"]
    for key in ("resume_pdf", "cover_letter_pdf"):   # the two fields the panel prints
        if ctx.get(key):
            ctx[key] = str(shown / Path(ctx[key]).name)
    win.apply_panel.show_application(ctx)
    win._apply_panel_open = True
    win.preview.setVisible(False)
    win._preview_shown = False
    win.apply_panel.show()
    sizes = win.hsplit.sizes()
    if len(sizes) >= 2 and sizes[-1] < 50:
        total = sum(sizes) or 1000
        win.hsplit.setSizes([max(420, total - 560), 560])


def _freeze_auto_reload(win: MainWindow) -> None:
    """Stop the dashboard reloading itself out from under a capture.

    The real window watches its source CSVs and polls every 15s, and both paths
    end in a reload from `csv_paths`, which is EMPTY here: the harness installs
    its synthetic frame directly. A capture that outruns the poll therefore gets
    a half-blank dashboard, and it did -- the walkthrough grew past 15s and its
    last four frames came back reading "0 jobs / 0 unseen" under a populated
    Settings tab.

    The reload ENTRY POINTS are what get neutered, not just the timers: every
    later `_apply_df_views` calls `_rearm_watcher`, so a freeze that only stopped
    the timers would be undone by the next line of fixture setup (it was, and the
    blank frames moved by one instead of going away).
    """
    for name in ("_poll_timer", "_reload_timer", "_scale_debounce"):
        timer = getattr(win, name, None)
        if timer is not None:
            timer.stop()
    win._auto_reload = lambda *a, **k: None
    win._poll_for_changes = lambda *a, **k: None
    win._on_fs_change = lambda *a, **k: None
    win._rearm_watcher = lambda *a, **k: None
    watcher = getattr(win, "_fs_watcher", None)
    if watcher is not None:
        if watcher.files():
            watcher.removePaths(watcher.files())
        if watcher.directories():
            watcher.removePaths(watcher.directories())


def _expand_settings_section(win: MainWindow, section: str = "Engine") -> None:
    """Unfold one non-secret Settings section so the grab shows real controls
    instead of ten folded title bars. Never Credentials: even with the synthetic
    .env its rows are secret fields, and an expanded secret row is a bad look in
    a README image."""
    sec = getattr(win.settings_tab, "_section_widgets", {}).get(section)
    if sec is not None:
        sec.set_collapsed(False)


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
    _freeze_auto_reload(win)
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
    stats_path = tmp_dir / "run_stats.csv"
    _write_run_stats(stats_path)
    from qt import main_window as _mw  # noqa: E402
    _mw.gdrive_root_dir = lambda _paths: tmp_dir
    # _refresh_stats renders from the CACHED frame `_load_frames` reads
    # off-thread; no loader runs here, so install it directly (patching
    # gdrive_root_dir alone leaves the tab on its "not synced yet" placeholder).
    win._stats_df = pd.read_csv(stats_path)
    win._apply_df_views()
    win._refresh_stats()
    _write_queue(queue_path, _queue_jobs())
    win.apply_queue_panel.refresh()
    _select_row0(win)
    _expand_settings_section(win)
    app.processEvents()
    written += _capture_all(app, win, prefix, "", SCALES)

    print(f"{written} PNG(s) written to {OUT_DIR} (prefix '{prefix}').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
