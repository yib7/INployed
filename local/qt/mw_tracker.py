"""The dashboard's tracker actions: marking jobs seen with undo, the row
context menu (open, status, block company, résumé folder), the tracker
extras (followed up, remove, export, import, interview prep) and the
Stats tab. `_TrackerActions` is a base of `MainWindow`, and its methods
run on a `MainWindow`.

Split out of `qt.main_window`.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from PySide6 import QtWidgets

import chrome_launch
import errmsg
import jobsdata
import osopen
from csv_io import read_csv_gz, write_csv_gz_atomic
from jobsdata import APPDATA
from qt import workers
from qt.stats_tab import _human_age
from qt.plaintext import literal
from qt.mw_tailor import _with_master_jd


class _TrackerActions:
    """`MainWindow`'s tracker actions (see the module docstring)."""

    # ---- mark seen (with undo / redo) ----------------------------------------

    def _write_is_seen(self, ids: list[str], value: str, paths=None) -> None:
        """Set is_seen=`value` for `ids` in whichever source CSV(s) hold them.
        Runs on the write queue's worker thread — `paths` is snapshotted on the
        UI thread at enqueue time so this never reads the mutable id_to_path map."""
        idset = set(ids)
        if paths is None:
            paths = {self.id_to_path[i] for i in ids if i in self.id_to_path}
        for path in paths:
            try:
                df = read_csv_gz(path)
                df["job_posting_id"] = df["job_posting_id"].astype(str)
                mask = df["job_posting_id"].isin(idset)
                if mask.any():
                    df.loc[mask, "is_seen"] = value
                    write_csv_gz_atomic(df, path)
            except (OSError, ValueError):
                pass

    def _apply_seen_locally(self, ids: list[str], value: str) -> None:
        """Optimistic seen-flip: update the in-memory frame + views instantly, then
        queue the CSV rewrite in the background (rewriting a ~27 MB gzipped CSV on
        the UI thread freezes the window)."""
        if not self.df.empty and "is_seen" in self.df.columns:
            idset = {str(i) for i in ids}
            mask = self.df["job_posting_id"].astype(str).isin(idset)
            self.df.loc[mask, "is_seen"] = value
        self._apply_df_views()
        paths = {self.id_to_path[i] for i in ids if i in self.id_to_path}
        ids = list(ids)
        self._enqueue_write(lambda: self._write_is_seen(ids, value, paths),
                            description=f"is_seen={value} on {len(ids)} job(s)")

    def _mark_ids_seen(self, ids: list[str], *, record_undo: bool = True) -> None:
        if not ids:
            return
        already = self.registry.all_ids()
        new_ids = [i for i in ids if i not in already]  # only the ones this click adds
        self.registry.mark(ids)   # registry write stays on the UI thread (fast, thread-affine)
        if record_undo and new_ids:
            self._seen_undo.append(new_ids)
            self._update_seen_buttons()
        self._apply_seen_locally(ids, "yes")

    def _mark_seen_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more rows to mark seen.")
            return
        self._mark_ids_seen(ids)
        self._set_status(f"Marked {len(ids)} job(s) as seen.")

    def _undo_seen(self) -> None:
        if not self._seen_undo:
            self._set_status("Nothing to undo.")
            return
        ids = self._seen_undo.pop()
        self.registry.unmark(ids)
        self._update_seen_buttons()
        self._apply_seen_locally(ids, "no")
        self._set_status(f"Undid 'seen' on {len(ids)} job(s).")

    def _update_seen_buttons(self) -> None:
        self.btn_undo_seen.setEnabled(bool(self._seen_undo))

    # ---- context-menu callbacks ----------------------------------------------

    def _open_url(self, jid: str) -> None:
        url = self._url_by_id.get(jid) or self._cell(self._row_for(jid), "url")
        if url:
            chrome_launch.open_in_chrome(url)

    def _set_status_for(self, ids: list[str], status: str) -> None:
        for jid in ids:
            row = self._row_for(jid)
            self.registry.set_status(jid, status, company=self._cell(row, "company_name"),
                                     job_title=self._cell(row, "job_title"),
                                     url=self._cell(row, "url"))
        if status == "applied":
            # Applying to a job means you've triaged it — also mark it seen (this
            # is what the old 'Mark applied' button did) and reload via that path.
            self._mark_ids_seen(ids)
        else:
            self._refresh_tracker()
        self._set_status(f"Set {len(ids)} job(s) to '{status}'.")

    def _block_company(self, company: str) -> None:
        try:
            jobsdata.append_to_blocklist(self.csv_paths, company)
        except OSError as exc:
            self._set_status(f"Could not block {company}: {errmsg.for_user(exc)}")
            return
        self.reload_data_async()
        self._set_status(f"Blocked {company}: hidden now and skipped on the next job search.")

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
            self._set_status("No tailored resume recorded; use 'Tailor resume' first.")
            return
        try:
            osopen.open_path(path)
        except OSError as e:
            self._set_status(f"Could not open {Path(path).name}: {errmsg.for_user(e)}")

    # ---- tracker extras ------------------------------------------------------

    def _tracker_followed_up(self) -> None:
        ids = self.tracker_tab.selected_ids()
        if not ids:
            self._set_status("Select tracker rows to mark followed up.")
            return
        self.registry.mark_followed_up(ids)
        self._refresh_tracker()
        self._set_status(f"Marked follow-up done on {len(ids)} job(s).")

    def _tracker_remove(self) -> None:
        ids = self.tracker_tab.selected_ids()
        if not ids:
            self._set_status("Select tracker rows to remove.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Remove from tracker?",
                literal(f"Remove {len(ids)} job(s) from the application tracker?")
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        for jid in ids:
            self.registry.clear_status(jid)
        self._refresh_tracker()
        self._set_status(f"Removed {len(ids)} job(s) from the tracker.")

    def _export_tracker(self) -> None:
        """Save a backup of the whole tracker DB (seen + statuses + résumé links)."""
        default = APPDATA / f"inployed-tracker-{date.today():%Y%m%d}.db"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export tracker", str(default), "SQLite database (*.db)")
        if not path:
            return
        try:
            dest = self.registry.export_to(Path(path))
        except Exception as e:  # noqa: BLE001 - surface any backup failure to the user
            self._set_status(f"Export failed: {errmsg.for_user(e)}")
            return
        self._set_status(f"Tracker exported → {dest}")

    def _import_tracker(self) -> None:
        """Merge a previously exported tracker backup into the current one."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import tracker", str(APPDATA),
            "SQLite database (*.db);;All files (*)")
        if not path:
            return
        if QtWidgets.QMessageBox.question(
                self, "Import tracker?",
                "Merge this backup into your current tracker? Existing entries are "
                "kept (a more recent status wins) and nothing is deleted."
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            counts = self.registry.import_from(Path(path))
        except Exception as e:  # noqa: BLE001 - surface any restore failure to the user
            self._set_status(f"Import failed: {errmsg.for_user(e)}")
            return
        self._refresh_tracker()
        QtWidgets.QMessageBox.information(
            self, "Tracker imported",
            literal(f"Merged {counts['status']} tracked application(s), {counts['seen']} "
            f"seen id(s), and {counts['resume_paths']} résumé link(s)."))

    def _tracker_prep(self) -> None:
        if getattr(self, "_prepping", False):
            return
        ids = self.tracker_tab.selected_ids() or self._selected_ids()
        if not ids:
            self._set_status("Select a job to generate an interview prep sheet for.")
            return
        job = self._job_payload(ids[0])
        if job is None:
            self._set_status("Job description not available; cannot build a prep sheet.")
            return
        resume_dir = self.registry.resume_path(ids[0])
        self._prepping = True
        self._apply_auth_env()
        self._set_status(f"Generating interview prep for {job['company_name']}: "
                         f"{job['job_title']} …")
        workers.run_async(self, lambda: self._prep_work(job, resume_dir),
                          on_done=self._finish_prep, on_error=self._finish_prep_error)

    def _prep_work(self, job: dict, resume_dir):
        from resume_tailor.prep import generate_prep_sheet
        job = _with_master_jd(job)      # the full description, on the worker
        out_dir = Path(resume_dir) if resume_dir and Path(resume_dir).exists() else None
        return generate_prep_sheet(job, out_dir)

    def _finish_prep(self, path) -> None:
        self._prepping = False
        self._set_status(f"Interview prep ready → {path}")
        try:
            osopen.open_path(path)
        except OSError:
            pass

    def _finish_prep_error(self, exc) -> None:
        self._prepping = False
        self._set_status(f"Interview prep FAILED: {errmsg.for_user(exc)}")

    # ---- stats + calibration -------------------------------------------------

    def _refresh_stats(self) -> None:
        """Render the Stats tab from the CACHED run_stats frame `_load_frames`
        read off-thread: this runs on every mark-seen/undo/delete
        repaint, so it must never do a synchronous Drive read itself."""
        stats_df = getattr(self, "_stats_df", None)
        summary = "run_stats.csv not synced yet; metrics appear after the next VM run."
        table_df = pd.DataFrame()
        newest = None
        if stats_df is not None and not stats_df.empty:
            table_df = stats_df.iloc[::-1].reset_index(drop=True)  # newest first
            summary = self._stats_summary(stats_df)
            try:
                newest = datetime.fromisoformat(
                    str(stats_df.iloc[-1].get("timestamp", "")).strip())
            except ValueError:
                newest = None
        self.stats_tab.set_stats(table_df, summary, self._calibration_text())
        threshold = int(getattr(self, "_stale_hours", 36) or 36)
        state, age = jobsdata.run_staleness(newest, datetime.now(), threshold)
        self.stats_tab.set_freshness(state, age)
        # Mirror the freshness onto the identity strip + status-bar summary.
        self._last_run_label = ("never" if age == float("inf")
                                else _human_age(age))
        label = ("Fresh: last run " if state == "fresh"
                 else "Stale: last run ") + self._last_run_label
        strip = getattr(self, "identity_strip", None)
        if strip is not None:
            strip.set_freshness(state, label)

    @staticmethod
    def _stats_summary(stats_df: pd.DataFrame) -> str:
        last = stats_df.iloc[-1]
        recent = stats_df.tail(7)
        empty = pd.Series(0, index=recent.index)
        tok = (pd.to_numeric(recent.get("prompt_tokens", empty), errors="coerce").fillna(0)
               + pd.to_numeric(recent.get("output_tokens", empty), errors="coerce").fillna(0))
        rows_in = pd.to_numeric(recent.get("rows_in", empty), errors="coerce").fillna(0)
        return (f"{len(stats_df)} run(s) logged · last: {last.get('timestamp', '?')}, "
                f"{last.get('rows_in', 0)} new, {last.get('llm_scored', 0)} scored · "
                f"7-run avg: {rows_in.mean():.0f} new, {tok.mean():,.0f} tokens/run")

    def _calibration_text(self) -> str:
        rows = self.registry.status_rows()
        if not rows:
            return ("Calibration: no labels yet; right-click a job -> Set status -> applied to "
                    "start building the applied-vs-recommendation dataset (target ~100 labels).")
        by_reco: Counter[str] = Counter()
        for r in rows:
            reco = self._cell(self._row_for(r["job_posting_id"]), "recommendation").strip().lower()
            by_reco[reco if reco in ("apply", "consider", "skip") else "unscored"] += 1
        parts = " · ".join(f"{k}: {v}" for k, v in by_reco.most_common())
        n = len(rows)
        note = ", enough to start tuning" if n >= 100 else f" (target ~100, at {n})"
        return f"Calibration: {n} labeled application(s){note} · by model reco: {parts}"
