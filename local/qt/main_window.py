"""The dashboard main window: the seven-tab QTabWidget + a score-preview pane and
the global action bar.

The three job tabs (High Score / All Jobs / Tracker) are real `JobsTab`s wired to
the data and registry; Stats / Resume Data / Apply Answers / Settings are filled in
later phases. Long-running actions (scrape, apply, tailor) run on a worker thread
via `qt.workers.run_async`. The score preview rides in a vertical splitter and is
shown only on the job tabs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtWidgets

import chrome
import jobsdata
import settings
from csv_io import read_csv_gz, reconcile_is_seen, write_csv_gz_atomic
from jobsdata import (
    ALL_COLUMNS,
    HIGH_SCORE_COLUMNS,
    TRACKER_COLUMNS,
    drop_blocklisted,
    filter_high_unseen,
    load_files,
    load_followup_days,
    load_hidden_columns,
    load_local_blocklist,
    load_min_score,
)
from qt import workers
from qt.jobs_tab import JobsTab
from qt.widgets import ScorePreview
from seen_db import SeenRegistry

TAB_TITLES = [
    "High Score (Unseen)",
    "All Jobs",
    "Tracker",
    "Stats",
    "Resume Data",
    "Apply Answers",
    "Settings",
]

# Tabs where a selected row has an analysis worth previewing.
PREVIEW_TABS = {"High Score (Unseen)", "All Jobs", "Tracker"}


class MainWindow(QtWidgets.QMainWindow):
    """Top-level window. `csv_paths` are the scored run files to load."""

    def __init__(self, csv_paths: list[Path] | None = None, registry=None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.csv_paths: list[Path] = list(csv_paths or [])
        self.registry = registry if registry is not None else SeenRegistry()
        self.setWindowTitle("INployed")
        self.setMinimumSize(1000, 660)

        self.min_score = load_min_score()
        self.followup_days = load_followup_days()
        self.hidden_columns = load_hidden_columns()
        self.df = pd.DataFrame()
        self.id_to_path: dict[str, Path] = {}
        self._row_by_id: dict[str, int] = {}
        self._url_by_id: dict[str, str] = {}
        self._tracked: dict[str, dict] = {}

        self._build()
        self.reload_data()
        self._apply_preview_visibility()

    # ---- construction --------------------------------------------------------

    def _make_jobs_tab(self, key: str, columns) -> JobsTab:
        return JobsTab(
            key, columns,
            on_open_url=self._open_url,
            on_set_status=self._set_status_for,
            on_block=self._block_company,
            on_selection=self._show_preview,
            hidden_columns=self.hidden_columns,
            save_hidden=self._save_hidden,
        )

    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(8, 8, 8, 8)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)

        self.high_tab = self._make_jobs_tab("high", HIGH_SCORE_COLUMNS)
        self.all_tab = self._make_jobs_tab("all", ALL_COLUMNS)
        self.tracker_tab = self._make_jobs_tab("tracker", TRACKER_COLUMNS)
        self._tab_widgets: dict[str, QtWidgets.QWidget] = {}
        pages = {"High Score (Unseen)": self.high_tab, "All Jobs": self.all_tab,
                 "Tracker": self.tracker_tab}
        for title in TAB_TITLES:
            page = pages.get(title) or QtWidgets.QWidget()
            self._tab_widgets[title] = page
            self.tabs.addTab(page, title)

        self.preview = ScorePreview()
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([720, 200])
        vbox.addWidget(self.splitter, 1)
        # Connect only now that self.preview exists (addTab above fires currentChanged).
        self.tabs.currentChanged.connect(lambda _i: self._apply_preview_visibility())

        vbox.addLayout(self._build_action_bar())
        self.setStatusBar(QtWidgets.QStatusBar())

    def _build_action_bar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        tip = QtWidgets.QLabel("Ctrl/Shift-click for multiple · double-click opens · "
                               "right-click for status / block")
        tip.setProperty("muted", True)
        bar.addWidget(tip)
        bar.addStretch(1)

        def button(text, slot, accent=False):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            if accent:
                b.setProperty("accent", True)
            bar.addWidget(b)
            return b

        self.btn_tailor = button("Tailor resume", self._tailor_selected, accent=True)
        button("Mark applied", self._mark_applied_selected)
        button("Mark seen (selected)", self._mark_seen_selected)
        button("Mark all shown seen", self._mark_all_shown_seen)
        button("Resume folder", self._open_resume_folder)
        button("Apply", self._apply_selected)
        button("Run scraper", self._run_scraper_dialog)
        button("Check setup", self._check_setup)
        button("Refresh", self.reload_data)
        return bar

    # ---- data ----------------------------------------------------------------

    def reload_data(self) -> None:
        df, id_to_path = load_files(self.csv_paths)
        df = drop_blocklisted(df, load_local_blocklist(self.csv_paths))
        self.id_to_path = id_to_path
        if not df.empty:
            if "is_seen" not in df.columns:
                df["is_seen"] = "no"
            df, _ = reconcile_is_seen(df, self.registry)
        self.df = df
        self._row_by_id = ({jid: i for i, jid in enumerate(df["job_posting_id"])}
                           if not df.empty else {})
        self._url_by_id = (dict(zip(df["job_posting_id"].astype(str), df["url"].astype(str)))
                           if not df.empty and "url" in df.columns else {})
        self.df_high = filter_high_unseen(df, self.min_score)
        resume_ids = self._resume_ids()
        self.high_tab.set_source_df(self.df_high, resume_ids)
        self.all_tab.set_source_df(df, resume_ids)
        self._refresh_tracker()
        total = 0 if df.empty else len(df)
        self._set_status(f"{total:,} jobs · {len(self.df_high)} unseen >=4")

    def _refresh_tracker(self) -> None:
        rows = self.registry.status_rows()
        self._tracked = {r["job_posting_id"]: r for r in rows}
        rpaths = set(self.registry.resume_paths())
        today = date.today()
        recs: list[dict] = []
        for r in rows:
            jid = r["job_posting_id"]
            row = self._row_for(jid)
            days = ""
            days_n = None
            if r.get("applied_date"):
                try:
                    days_n = (today - date.fromisoformat(r["applied_date"])).days
                    days = str(days_n)
                except ValueError:
                    pass
            follow = ""
            if r.get("followed_up_at"):
                follow = "done"
            elif (r["status"] == "applied" and days_n is not None
                  and days_n >= self.followup_days):
                follow = "DUE"
            recs.append({
                "job_posting_id": jid,
                "status": r["status"],
                "status_date": r.get("status_date") or "",
                "applied_date": r.get("applied_date") or "",
                "days": days,
                "follow_up": follow,
                "score": self._cell(row, "score"),
                "deep_score": self._cell(row, "deep_score"),
                "job_title": r.get("job_title") or self._cell(row, "job_title"),
                "company_name": r.get("company") or self._cell(row, "company_name"),
                "url": r.get("url") or self._cell(row, "url"),
                "resume": "✓" if jid in rpaths else "",
            })
        cols = [c for c, _ in TRACKER_COLUMNS] + ["job_posting_id"]
        tdf = pd.DataFrame(recs) if recs else pd.DataFrame(columns=cols)
        self.tracker_tab.set_source_df(tdf, self._resume_ids())

    def _resume_ids(self) -> frozenset:
        try:
            return frozenset(self.registry.resume_paths())
        except Exception:  # noqa: BLE001 - cosmetic; never break the view
            return frozenset()

    # ---- row helpers ---------------------------------------------------------

    def _row_for(self, jid: str):
        i = self._row_by_id.get(jid)
        if i is None or self.df.empty:
            return None
        try:
            return self.df.iloc[i]
        except (IndexError, KeyError):
            return None

    @staticmethod
    def _cell(row, col: str) -> str:
        if row is None:
            return ""
        v = row.get(col, "")
        return "" if pd.isna(v) else str(v)

    def _job_payload(self, jid: str) -> dict | None:
        row = self._row_for(jid)
        if row is None:
            return None
        return {
            "job_posting_id": jid,
            "company_name": self._cell(row, "company_name"),
            "job_title": self._cell(row, "job_title"),
            "job_description_formatted": self._cell(row, "job_description_formatted"),
            "job_description": self._cell(row, "job_description"),
            "job_summary": self._cell(row, "job_summary"),
            "url": self._cell(row, "url"),
        }

    def _active_jobs_tab(self) -> JobsTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, JobsTab) else None

    def _selected_ids(self) -> list[str]:
        tab = self._active_jobs_tab()
        return tab.selected_ids() if tab else []

    # ---- preview -------------------------------------------------------------

    def _apply_preview_visibility(self) -> None:
        title = self.tabs.tabText(self.tabs.currentIndex())
        show = title in PREVIEW_TABS
        self.preview.setVisible(show)
        self._preview_shown = show

    def _show_preview(self, jid: str) -> None:
        if not jid:
            self.preview.show_segments([])
            return
        segs = jobsdata.job_detail_segments(self._row_for(jid), self._tracked.get(jid))
        self.preview.show_segments(segs)

    # ---- mark seen / applied -------------------------------------------------

    def _mark_ids_seen(self, ids: list[str]) -> None:
        if not ids:
            return
        self.registry.mark(ids)
        idset = set(ids)
        for path in {self.id_to_path[i] for i in ids if i in self.id_to_path}:
            try:
                df = read_csv_gz(path)
                df["job_posting_id"] = df["job_posting_id"].astype(str)
                mask = df["job_posting_id"].isin(idset)
                if mask.any():
                    df.loc[mask, "is_seen"] = "yes"
                    write_csv_gz_atomic(df, path)
            except (OSError, ValueError):
                pass
        self.reload_data()

    def _mark_seen_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark seen.")
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked {len(ids)} job(s) as seen.")

    def _mark_all_shown_seen(self) -> None:
        tab = self._active_jobs_tab()
        if not tab:
            return
        ids = [jid for jid in (tab.model.job_id(r) for r in range(tab.model.rowCount())) if jid]
        if not ids:
            self._set_status("Nothing shown to mark.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Mark all as seen?",
                f"Mark all {len(ids)} currently shown jobs as seen?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked all {len(ids)} shown job(s) as seen.")

    def _mark_applied_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark applied.")
            return
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(jid, "applied", company=self._cell(row, "company_name"),
                                     job_title=self._cell(row, "job_title"),
                                     url=self._cell(row, "url"))
        self._mark_ids_seen(ids)
        self._set_status(f"Marked {len(ids)} job(s) as applied — see the Tracker tab.")

    # ---- context-menu callbacks ----------------------------------------------

    def _open_url(self, jid: str) -> None:
        url = self._url_by_id.get(jid) or self._cell(self._row_for(jid), "url")
        if url:
            chrome.open_in_chrome(url)

    def _set_status_for(self, ids: list[str], status: str) -> None:
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(jid, status, company=self._cell(row, "company_name"),
                                     job_title=self._cell(row, "job_title"),
                                     url=self._cell(row, "url"))
        self._refresh_tracker()
        self._set_status(f"Set {len(ids)} job(s) to '{status}'.")

    def _block_company(self, company: str) -> None:
        try:
            jobsdata.append_to_blocklist(self.csv_paths, company)
        except OSError as exc:
            self._set_status(f"Could not block {company}: {exc}")
            return
        self.reload_data()
        self._set_status(f"Blocked {company} — hidden now and skipped on the next scrape.")

    def _save_hidden(self, key: str, hidden: list[str]) -> None:
        self.hidden_columns[key] = list(hidden)
        jobsdata.save_hidden_columns(self.hidden_columns)

    def _open_resume_folder(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select a row to open its resume folder.")
            return
        path = self.registry.resume_path(ids[0])
        if not path or not Path(path).exists():
            self._set_status("No tailored resume recorded — use 'Tailor resume' first.")
            return
        try:
            os.startfile(path)  # noqa: S606
        except OSError as e:
            self._set_status(f"Could not open {path}: {e}")

    # ---- run scraper (spend-guarded) -----------------------------------------

    @staticmethod
    def scraper_cmd(bounded: bool) -> list[str]:
        cmd = [sys.executable, "scraper.py"]
        if bounded:
            cmd += ["--max-keywords", "1", "--limit", "5"]
        return cmd

    @staticmethod
    def scorer_cmd() -> list[str]:
        return [sys.executable, "score_jobs.py"]

    def _confirm_scrape(self) -> str | None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Run scraper")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText("Running the scraper collects fresh jobs from Bright Data — this spends real "
                    "money.\n\n- Small test run: 1 keyword, 5 postings/search (cheap check).\n"
                    "- Full run: your full search config (normal daily cost).\n\nIt then scores "
                    "the new jobs and refreshes the dashboard.")
        small = box.addButton("Small test run", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        full = box.addButton("Full run", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is small:
            return "bounded"
        if clicked is full:
            return "full"
        return None

    def _run_scraper_dialog(self) -> None:
        if getattr(self, "_scraping", False):
            self._set_status("A scrape is already running.")
            return
        choice = self._confirm_scrape()
        if not choice:
            return
        self._scraping = True
        self._set_status("Starting scraper … (progress in the console)")
        workers.run_async(self, lambda: self._scrape_work(choice == "bounded"),
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def _scrape_work(self, bounded: bool):
        repo = Path(__file__).resolve().parents[2]
        for cmd in (self.scraper_cmd(bounded), self.scorer_cmd()):
            proc = subprocess.Popen(cmd, cwd=str(repo))
            if proc.wait() != 0:
                raise RuntimeError(f"{cmd[1]} failed — check the console for the error.")
        return True

    def _after_scrape(self, _result) -> None:
        self._scraping = False
        self.reload_data()
        self._set_status("Scrape + score complete — dashboard refreshed.")

    def _after_scrape_error(self, exc) -> None:
        self._scraping = False
        self._set_status(f"Run scraper failed: {exc}")

    # ---- apply (open posting for review; never submits) -----------------------

    def _apply_selected(self) -> None:
        if getattr(self, "_applying", False):
            return
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select a job to open its application.")
            return
        jid = ids[0]
        payload = self._job_payload(jid)
        self._applying = True
        self._set_status("Opening application …")
        workers.run_async(self, lambda: self._apply_work(jid, payload),
                          on_done=self._finish_apply_ok, on_error=self._finish_apply_error)

    def _apply_work(self, jid: str, payload: dict | None):
        from resume_tailor import apply as apply_mod
        folder = apply_mod.resolve_generated_dir(job_id=jid, job=payload)
        ctx = apply_mod.build_apply_context(folder)
        url = ctx.get("apply_url", "")
        if url:
            try:
                chrome.open_in_chrome(url)
            except Exception:  # noqa: BLE001
                pass
        return ctx

    def _finish_apply_ok(self, ctx: dict) -> None:
        self._applying = False
        job = ctx.get("job") or {}
        resume_pdf = ctx.get("resume_pdf", "")
        self._set_status(f"Application opened for {job.get('company', '?')} — "
                         f"{job.get('title', '?')}. Review before submitting.")
        if resume_pdf:
            QtWidgets.QApplication.clipboard().setText(resume_pdf)
        body = (f"Company : {job.get('company', '?')}\n"
                f"Role    : {job.get('title', '?')}\n"
                f"Apply   : {ctx.get('apply_url') or '(none)'}\n\n"
                f"Résumé PDF (copied to clipboard):\n{resume_pdf or '(missing)'}\n")
        if ctx.get("cover_letter_pdf"):
            body += f"\nCover letter:\n{ctx['cover_letter_pdf']}\n"
        body += ("\nReview every field. Submission is left to you.\n"
                 "Run the apply-to-job skill in Claude-in-Chrome to fill the form.")
        QtWidgets.QMessageBox.information(self, "Apply — review before submitting", body)

    def _finish_apply_error(self, exc) -> None:
        self._applying = False
        msg = str(exc)
        self._set_status(msg.splitlines()[0] if msg else "Apply failed")
        QtWidgets.QMessageBox.information(
            self, "Apply", f"{msg}\n\nUse 'Tailor resume' on this job, then try Apply again.")

    # ---- tailor --------------------------------------------------------------

    def _tailor_selected(self) -> None:
        if getattr(self, "_tailoring", False):
            return
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more jobs to tailor a resume for.")
            return
        jobs = [j for j in (self._job_payload(i) for i in ids) if j]
        if not jobs:
            self._set_status("Could not find job data for the selection.")
            return
        cfg = settings.load()
        cover = QtWidgets.QMessageBox.question(
            self, "Cover letter",
            f"Also generate a cover letter for the selected {len(jobs)} job(s)?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes
        opts = {"cover_letter": cover, "ats_report": bool(cfg.get("tailor_ats_report", True)),
                "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                "tone": cfg.get("resume_tone", "professional")}
        self._tailoring = True
        self.btn_tailor.setEnabled(False)
        self._set_status(f"Tailoring {len(jobs)} resume(s) … (progress in the console)")
        workers.run_async(self, lambda: self._tailor_work(jobs, opts),
                          on_done=self._finish_tailor, on_error=self._finish_tailor_error)

    def _tailor_work(self, jobs: list[dict], opts: dict):
        from resume_tailor import tailor as tailor_resume
        last_dir = None
        for job in jobs:
            last_dir = tailor_resume(job, cover_letter=opts["cover_letter"],
                                     ats_report=opts["ats_report"], prep_sheet=opts["prep_sheet"],
                                     tone=opts["tone"])
            if last_dir and job.get("job_posting_id"):
                try:
                    with SeenRegistry() as reg:
                        reg.record_resume(job["job_posting_id"], str(last_dir))
                except Exception:  # noqa: BLE001 - bookkeeping only
                    pass
        return last_dir

    def _finish_tailor(self, out_dir) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        if out_dir:
            self._set_status(f"Resume(s) ready → {out_dir}")
            try:
                os.startfile(str(out_dir))  # noqa: S606
            except OSError:
                pass
        self.reload_data()

    def _finish_tailor_error(self, exc) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        self._set_status(f"Tailor failed: {exc}")

    # ---- check setup ---------------------------------------------------------

    def _check_setup(self) -> None:
        from resume_tailor import master_validate
        try:
            result = master_validate.check_setup()
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Check setup", f"Could not run checks: {exc}")
            return
        problems: list[str] = []
        for label, errs in (("Resume data", result.get("master", [])),
                            ("Apply answers", result.get("answers", []))):
            problems.extend(f"[{label}] {e}" for e in errs)
        try:
            auth = jobsdata._load_cfg().get("gemini_auth", "vertex")
            stored = settings.load()
            project = stored.get("GOOGLE_CLOUD_PROJECT", "") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT", "")
            has_key = settings.secret_status().get("RESUME_TAILOR_GEMINI_API_KEY", False) or bool(
                os.environ.get("RESUME_TAILOR_GEMINI_API_KEY"))
            problems.extend(f"[Engine] {w}"
                            for w in jobsdata._engine_credential_warnings(auth, project, has_key))
        except Exception:  # noqa: BLE001
            pass
        if not problems:
            QtWidgets.QMessageBox.information(
                self, "Check setup", "All good — no problems found.")
            self._set_status("Setup check passed.")
        else:
            QtWidgets.QMessageBox.critical(
                self, "Check setup", "Problems found:\n\n- " + "\n- ".join(problems))
            self._set_status(f"Setup check: {len(problems)} problem(s) — see the list.")

    # ---- misc ----------------------------------------------------------------

    def _set_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg)

    def tab_count(self) -> int:
        return self.tabs.count()

    def tab_titles(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]
