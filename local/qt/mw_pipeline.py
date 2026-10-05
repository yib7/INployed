"""The dashboard's pipeline actions: the scraper and scorer commands and
the spend-guarded run, pushing seen ids and the outbox to the VM, adding
a job by hand, and deleting or editing a hand-added job.
`_PipelineActions` is a base of `MainWindow`, and its methods run on a
`MainWindow`.

Split out of `qt.main_window`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtWidgets

import errmsg
import jev_switch
import jobsdata
import settings
from jobsdata import APPDATA, gdrive_root_dir
from qt import workers
from qt.manual_add_dialog import ManualAddDialog
from qt.plaintext import literal
from resume_trash import recycle_resume_folder


def _console_python(exe: str | None = None) -> str:
    """The console Python to run child scripts with.

    The dashboard launches under ``pythonw.exe`` (no console window), whose
    ``sys.executable`` is ``pythonw.exe``. A child spawned with that has no real
    stdout, so its output — and any error — vanishes. Swap to the sibling
    ``python.exe`` so the scraper/scorer have capturable stdio.
    """
    exe = exe or sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _no_window_flag() -> int:
    """CREATE_NO_WINDOW on Windows (don't flash a console for the captured child);
    0 everywhere else."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _scorer_jev_switch() -> str:
    """SCORE_USE_JEV for a scorer the dashboard launches, in the form
    `jev_score.use_jev` reads: "0" when a Settings switch turns Jev scoring off
    (the master switch or the scoring switch: `jev_switch.switched_off`),
    else "1". The key, the SDK and `local/jev.py` are the scorer's to check: it
    loads `.env` first, so it finds a key saved in Settings, and when a piece is
    missing it prints the one warning that names it. Reads config.json, so call
    it off the UI thread."""
    return "0" if jev_switch.switched_off("scoring") else "1"


class _PipelineActions:
    """`MainWindow`'s pipeline actions (see the module docstring)."""

    # ---- run scraper (spend-guarded) -----------------------------------------

    # -u: the children write to a pipe, which Python block-buffers — without it
    # scrape.log stays empty for the whole run and a healthy scrape looks dead.
    @staticmethod
    def scraper_cmd(bounded: bool) -> list[str]:
        cmd = [_console_python(), "-u", "pipeline/scraper.py"]
        if bounded:
            cmd += ["--max-keywords", "1", "--limit", "5"]
        return cmd

    @staticmethod
    def scorer_cmd() -> list[str]:
        return [_console_python(), "-u", "pipeline/score_jobs.py"]

    @staticmethod
    def _cmd_label(cmd: list[str]) -> str:
        """Name the script a failed command ran, for the error dialog.

        cmd[-1] is only the script on an UNBOUNDED run; a bounded scrape ends with
        "--limit 5", and naming that would open the dialog with "5 failed (exit 1)".
        Find the .py, and fall back to cmd[-1] if there isn't one."""
        for part in cmd:
            if part.endswith(".py"):
                return Path(part).name
        return Path(cmd[-1]).name if cmd else "command"

    @staticmethod
    def _scrape_log_path() -> Path:
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return APPDATA / "scrape.log"

    def _confirm_scrape(self) -> str | None:
        box = QtWidgets.QMessageBox(self)
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setWindowTitle("Find new jobs")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText("Finding new jobs collects fresh postings through the job-data provider; this "
                    "spends real money / API credits.\n\n- Small test run: 1 keyword, 5 postings/search "
                    "(cheap check).\n- Full run: your full search config (normal daily cost).\n\nIt then "
                    "scores the new jobs and refreshes the dashboard.")
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

    def _pipeline_busy(self) -> str | None:
        """Why the scrape/score pipeline is unavailable, or None when it's free.

        A scrape run and a manual add share scrape.log and the post-run outbox
        push, so they are mutually exclusive: whichever is in flight blocks the
        other. One check, used by every entry point."""
        if getattr(self, "_scraping", False):
            return "A job search is already running."
        if getattr(self, "_manual_adding", False):
            return "A manual job add is still running."
        return None

    def _run_scraper_dialog(self) -> None:
        busy = self._pipeline_busy()
        if busy:
            self._set_status(busy)
            return
        choice = self._confirm_scrape()
        if not choice:
            return
        self._scraping = True
        self._set_status(f"Finding new jobs … progress in {self._scrape_log_path()}")
        workers.run_async(self, lambda: self._scrape_work(choice == "bounded"),
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def offer_unscored_recovery(self) -> None:
        """Offer to score run CSVs an interrupted scrape left behind.

        The scrape pipeline (scraper -> scorer -> refresh) runs inside this
        process, so closing the dashboard mid-run orphans it: the collected
        `<label>/<run>.csv` survives on disk but was never scored, and unscored
        CSVs are invisible to the dashboard. Called from app.main() once at
        startup (never from __init__ — a modal there would hang headless tests).
        """
        if self._pipeline_busy():
            return
        try:
            pending = jobsdata.unscored_run_csvs()
        except OSError:
            return
        if not pending:
            return
        names = "\n".join(f"  •  {p.parent.name}/{p.name}" for p in pending)
        resp = QtWidgets.QMessageBox.question(
            self, "Unscored job-search results found",
            literal("A previous job search was interrupted before its results were "
            "scored, so they never appeared in the dashboard:\n\n"
            f"{names}\n\n"
            "Score them now? This only runs the scoring step (Gemini); it does "
            "not collect new jobs and costs no discovery credits."),
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes)
        if resp != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._scraping = True
        self._set_status(
            f"Scoring recovered job-search results … progress in {self._scrape_log_path()}")
        workers.run_async(self, self._score_only_work,
                          on_done=self._after_scrape, on_error=self._after_scrape_error)

    def _scorer_env(self) -> dict:
        """Environment for a score_jobs.py this dashboard launches: a copy of ours with
        SCORE_USE_JEV set from the Settings switch (`_scorer_jev_switch`). The scorer
        reads that variable before any config.json, so a scorer that finds no config
        beside it still follows the dashboard. Set on the child's env only."""
        env = os.environ.copy()
        env["SCORE_USE_JEV"] = _scorer_jev_switch()
        return env

    def _scrape_env(self) -> dict:
        """Environment for the local scrape subprocess: `_scorer_env()`, plus a pointer to
        the synced Drive master so the scraper also excludes — and never re-bills — jobs
        the VM already collected. The local repo master is only a small stub of recent
        local runs, so without this a local 'Find new jobs' run re-pulls (and re-scores)
        postings the VM already has. Set on the CHILD's env only, not our own process, so
        the post-scrape VM-push set stays lean — it carries what THIS host collected, not
        the Drive master pulled down from the VM."""
        env = self._scorer_env()
        root = gdrive_root_dir(self.csv_paths)
        if root is not None:
            master = Path(root) / "linkedin_jobs_master.csv.gz"
            if master.exists():
                # scraper.EXTRA_MASTER_ENV — the synced Drive master to also exclude from.
                env["LINKEDIN_EXTRA_MASTER"] = str(master)
        return env

    def _scrape_work(self, bounded: bool):
        """Run scraper.py then score_jobs.py, streaming their output to scrape.log.

        Output is captured (the dashboard runs under pythonw with no console) so a
        failure surfaces the real error instead of a dead 'check the console'.
        """
        log_path = self._scrape_log_path()
        env = self._scrape_env()
        before = self._outbox_snapshot()
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            self._run_pipeline((self.scraper_cmd(bounded), self.scorer_cmd()),
                               log, log_path, env=env)
            # Both steps succeeded: push the freshly-collected ids to the VM (if
            # configured) so its next scheduled run doesn't re-collect — and re-bill —
            # what this run just pulled. Best-effort: never fail the scrape over a sync.
            self._push_seen_ids_to_vm(log)
            self._push_outbox_to_vm(log, before)
        return True

    def _score_only_work(self):
        """Recovery worker: run ONLY score_jobs.py (it picks up the newest unscored
        run CSV itself). Appends to scrape.log — the earlier, interrupted run's
        output is the context for what is being recovered, so keep it."""
        log_path = self._scrape_log_path()
        before = self._outbox_snapshot()
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            self._run_pipeline((self.scorer_cmd(),), log, log_path, env=self._scorer_env())
            # The recovered run's ids/rows never made it to the VM either — they
            # ride the same post-scrape sync as a normal run, or the recovery
            # stays local-only.
            self._push_seen_ids_to_vm(log)
            self._push_outbox_to_vm(log, before)
        return True

    def _run_pipeline(self, cmds, log, log_path, env=None) -> None:
        """Run each command with the repo root as cwd, streaming combined
        stdout+stderr to `log`; raise RuntimeError carrying the output tail when
        one exits non-zero (the dashboard runs under pythonw — the log is the
        only console there is)."""
        repo = Path(__file__).resolve().parents[2]
        for cmd in cmds:
            log.write(f"\n=== {' '.join(cmd)} ===\n")
            log.flush()
            proc = subprocess.Popen(
                cmd, cwd=str(repo), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=_no_window_flag(), env=env)
            captured: list[str] = []
            if proc.stdout is not None:
                for line in proc.stdout:
                    captured.append(line)
                    log.write(line)
                    log.flush()
            rc = proc.wait()
            if rc != 0:
                tail = "".join(captured).strip().splitlines()[-15:]
                raise RuntimeError(
                    f"{self._cmd_label(cmd)} failed (exit {rc}).\n\n"
                    + ("\n".join(tail) if tail else "(no output captured)")
                    + f"\n\nFull log: {log_path}")

    @staticmethod
    def _push_seen_ids_to_vm(log) -> None:
        """Best-effort post-scrape sync: write this host's exclude-id set and scp it
        to the VM when one is configured. Any failure (no VM, gcloud error, file error)
        is logged to scrape.log and swallowed — the scrape result is unaffected."""
        try:
            repo = Path(__file__).resolve().parents[2]
            for _p in (str(repo / "pipeline"), str(repo / "local")):
                if _p not in sys.path:
                    sys.path.insert(0, _p)
            import scraper
            import vm_sync
            target = vm_sync.VMTarget.from_env()
            if not target.configured():
                log.write("\n=== VM seen-id sync: no VM configured, skipped ===\n")
                log.flush()
                return
            # Stage the push artifact in outbox/, NOT next to the scraper. The local
            # scraper reads OUTPUT_DIR/external_exclude_ids.json as "ids collected on
            # ANOTHER machine", so writing our own load_exclude_ids() there fed the
            # file straight back into the next run's exclude set — and those ids are
            # deliberately NOT windowed, so the exclusion array ratcheted up
            # monotonically (to the whole 2,679-row master) until Bright
            # Data rejected every input with child_input_size_validation and the
            # scrape silently collected nothing. vm_sync only ever pushes this file
            # OUT (see push_exclude_ids_cmd), never pulls one in, so on this host it
            # has no business being read at all. The remote filename is fixed by
            # vm_sync.EXCLUDE_REMOTE_FILE, so the local name is free to move.
            #
            # Ask the outbox module for its own directory rather than rebuilding
            # `repo / "outbox"` here. Same path, but one owner: a second copy of
            # the expression is a copy the conftest redirect does not cover, so
            # this line was mkdir'ing into the real repo during the test suite.
            import outbox
            outbox_dir = outbox.OUTBOX_DIR
            outbox_dir.mkdir(parents=True, exist_ok=True)
            path = scraper.write_external_exclude_ids(
                outbox_dir / "external_exclude_ids.json")
            log.write(f"\n=== VM seen-id sync: pushing {path.name} to VM ===\n")
            log.flush()
            res = vm_sync.sync_exclude_ids_to_vm(target, path)
            if res is not None and res.returncode == 0:
                log.write("VM seen-id sync: OK\n")
            else:
                rc = getattr(res, "returncode", "n/a")
                err = (getattr(res, "stderr", "") or getattr(res, "stdout", "")).strip()
                log.write(f"VM seen-id sync: FAILED (exit {rc}) {err}\n")
            log.flush()
        except Exception as e:  # noqa: BLE001 - sync is best-effort, never fail the scrape
            try:
                log.write(f"\n=== VM seen-id sync: error ({e}); scrape unaffected ===\n")
                log.flush()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _outbox_snapshot() -> dict:
        """Pre-scrape {run-file: mtime} so the post-scrape hook can tell which run
        files this scrape produced/rewrote. {} on any failure (hook degrades to
        push-retries-only)."""
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            return outbox.snapshot_run_files()
        except Exception:  # noqa: BLE001 - best-effort
            return {}

    def _push_outbox_to_vm(self, log, before: dict) -> None:
        """Best-effort post-scrape data sync: queue this run's new master rows (+ the
        run-stats file) in the outbox and push every pending outbox file to the VM's
        ~/incoming/. Also sweeps the local master for rows the Drive master lacks
        (outbox.unsynced_master_ids) so rows collected outside this hook — a CLI
        snapshot recovery, a push that never got retried — are queued too instead of
        staying stranded on this PC. Failures are logged to scrape.log and swallowed —
        a sync problem never fails a scrape. Push still runs when the run added
        nothing, so files queued by earlier failed pushes retry here."""
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            import vm_sync
            ids = outbox.new_run_ids(before)
            root = gdrive_root_dir(self.csv_paths)
            if root is not None:
                have = set(ids)
                for jid in outbox.unsynced_master_ids(
                        Path(root) / "linkedin_jobs_master.csv.gz"):
                    if jid not in have:
                        ids.append(jid)
                        have.add(jid)
            if ids:
                path = outbox.write_rows_outbox(ids)
                log.write(f"\n=== outbox: queued {len(ids)} row id(s) -> "
                          f"{getattr(path, 'name', None)} ===\n")
            else:
                log.write("\n=== outbox: no new rows this run ===\n")
            outbox.write_stats_outbox()
            target = vm_sync.VMTarget.from_env()
            pushed, kept = outbox.push_outbox(target, log=log)
            log.write(f"outbox push done: {pushed} pushed, {kept} kept\n")
            log.flush()
        except Exception as e:  # noqa: BLE001 - sync is best-effort
            try:
                log.write(f"\n=== outbox sync: error ({e}); scrape unaffected ===\n")
                log.flush()
            except Exception:  # noqa: BLE001
                pass

    def _after_scrape(self, _result) -> None:
        self._scraping = False
        # A local scrape writes to the repo dir, not the synced Drive folder this
        # window was opened against — fold the new scored run file(s) into the
        # sources so the freshly scraped jobs actually appear.
        for p in jobsdata.local_run_files():
            if p not in self.csv_paths:
                self.csv_paths.append(p)
        self.reload_data_async()
        self._set_status("Job search + score complete; dashboard refreshed.")

    def _after_scrape_error(self, exc) -> None:
        self._scraping = False
        msg = errmsg.for_user(exc)
        self._set_status(f"Find new jobs failed: {msg.splitlines()[0] if msg else exc}")
        QtWidgets.QMessageBox.critical(self, "Find new jobs", literal(f"The run failed.\n\n{msg}"))

    # ---- add a job by hand (no scraper, no scoring) ----------------------------

    def _add_manual_job_dialog(self) -> None:
        """Open the manual-entry form, then run parse->tailor->append off-thread.

        The user already chose this job: there is no scoring step, it
        goes straight to the same tailoring (resume_tailor) pipeline a scraped job
        gets. A duplicate check runs BEFORE any of that, against both the
        rows already loaded and the master file, so re-adding the same posting
        never spends a fresh tailor call. The heavy work runs on a worker thread
        so the window never freezes."""
        busy = self._pipeline_busy()
        if busy:
            self._set_status(busy)
            return
        dlg = ManualAddDialog(self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        import manual_add
        dup = manual_add.find_duplicate(vals.get("jd_text", ""), vals.get("url", ""),
                                        df=self.df)
        if dup is not None:
            if self._confirm_retailor_duplicate(dup):
                self._start_manual_pipeline(
                    lambda opts: self._manual_retailor_work(
                        dup, vals.get("jd_text", ""), opts),
                    verb="Tailoring again")
            return
        self._start_manual_pipeline(
            lambda opts: self._manual_add_work(vals, opts), verb="Adding job")

    def _confirm_retailor_duplicate(self, dup: dict) -> bool:
        """The duplicate prompt: tailor the existing row again, or cancel. A
        duplicate never silently reports success."""
        import manual_add
        box = QtWidgets.QMessageBox(self)
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setWindowTitle("Add a job by hand")
        box.setText(manual_add.duplicate_message(dup))
        again = box.addButton("Tailor again", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is again

    def _start_manual_pipeline(self, work_fn, *, verb: str) -> None:
        """Shared setup for a fresh add and a "Tailor again": the cover-letter
        prompt, busy flag and worker-thread dispatch are identical either way."""
        cfg = settings.load()
        cover = QtWidgets.QMessageBox.question(
            self, "Cover letter", "Also generate a cover letter for this job?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes
        opts = {"cover_letter": cover, "ats_report": bool(cfg.get("tailor_ats_report", True)),
                "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                "tone": cfg.get("resume_tone", "professional")}
        self._manual_adding = True
        self._apply_auth_env()
        self._set_status(f"{verb}: tailoring …")
        workers.run_async(self, lambda: work_fn(opts),
                          on_done=self._finish_manual_add, on_error=self._finish_manual_add_error)

    def _manual_add_work(self, vals: dict, opts: dict) -> dict:
        """Worker body: the toolkit-agnostic manual_add pipeline. The LLM/scraper
        seams default to the real implementations (mockable in tests)."""
        import manual_add
        res = manual_add.add_manual_job(
            jd_text=vals.get("jd_text", ""), url=vals.get("url", ""),
            company=vals.get("company", ""), title=vals.get("title", ""),
            tailor_opts=opts, on_status=self.tailor_progress.emit)
        if not res.get("appended"):
            # the posting was already in the master: no new row to send to the VM
            return dict(res, duplicate=True)
        self._push_manual_row_to_vm(res)
        return res

    def _manual_retailor_work(self, record: dict, jd_text: str, opts: dict) -> dict:
        """Worker body for "Tailor again": re-run tailoring on an already
        saved row, never appending a second copy. `jd_text` is the description
        the user just re-pasted into the dialog; retailor_existing uses it only
        to patch a blank stored description for this run (a hand-added job's
        dashboard row carries none), and never rewrites the row with it."""
        import manual_add
        return manual_add.retailor_existing(
            record, jd_text=jd_text, tailor_opts=opts, on_status=self.tailor_progress.emit)

    def _push_manual_row_to_vm(self, res: dict) -> None:
        # Queue + push the new master row to the VM (best-effort — never fail the add).
        try:
            repo = Path(__file__).resolve().parents[2]
            if str(repo / "local") not in sys.path:
                sys.path.insert(0, str(repo / "local"))
            import outbox
            import vm_sync
            jid = str((res.get("record") or {}).get("job_posting_id", "")).strip()
            with open(self._scrape_log_path(), "a", encoding="utf-8",
                      errors="replace") as log:
                log.write("\n=== manual add: outbox sync ===\n")
                if jid:
                    outbox.write_rows_outbox([jid])
                outbox.push_outbox(vm_sync.VMTarget.from_env(), log=log)
        except Exception:  # noqa: BLE001 - sync is best-effort
            pass

    def _finish_manual_add(self, result: dict) -> None:
        self._manual_adding = False
        result = result or {}
        rec = result.get("record") or {}
        if result.get("resume_dir") and rec.get("job_posting_id"):
            try:
                self.registry.record_resume(rec["job_posting_id"], str(result["resume_dir"]))
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass
        # Fold the manual scored gz into the sources so the new job appears now and
        # survives a restart, same bridge a local scrape gets.
        for p in jobsdata.local_run_files():
            if p not in self.csv_paths:
                self.csv_paths.append(p)
        self.reload_data_async()
        title = rec.get("job_title", "job")
        company = rec.get("company_name", "")
        if result.get("duplicate"):
            lead = f"Already in your jobs, so not added again: {title} @ {company}."
            self._set_status(
                f"{lead} Tailored it again." if result.get("resume_dir") else
                f"{lead} Tailoring failed; retry with Tailor résumé on the job.")
        elif result.get("resume_dir"):
            self._set_status(f"Manual job tailored: {title} @ {company}.")
        else:
            # tailoring failed but the row is still saved; say how to retry.
            self._set_status(
                f"Manual job saved but tailoring failed: {title} @ {company}. "
                "Retry with Tailor résumé on the job.")

    def _finish_manual_add_error(self, exc) -> None:
        self._manual_adding = False
        msg = errmsg.for_user(exc)
        self._set_status(f"Add job failed: {msg.splitlines()[0] if msg else exc}")
        QtWidgets.QMessageBox.warning(self, "Add a job by hand", literal(f"Could not add the job.\n\n{msg}"))

    # ---- delete / edit job entries -------------------------------------------

    def _delete_jobs(self, ids) -> None:
        """Permanently remove the selected job(s) from the dataset (confirm first).
        Any tailored-résumé folder goes to the Recycle Bin (recoverable)."""
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if not ids:
            return
        if QtWidgets.QMessageBox.question(
                self, "Delete job(s)?",
                literal(f"Permanently remove {len(ids)} job(s) from your dataset? This can't be "
                "undone. Any tailored-résumé folder is moved to the Recycle Bin.")
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        # Snapshot the résumé folders BEFORE the delete — the registry rows are
        # cleared below, and the folder must still be findable afterwards.
        folders = {}
        for jid in ids:
            try:
                folders[jid] = self.registry.resume_path(jid)
            except Exception:  # noqa: BLE001 - bookkeeping only
                folders[jid] = None
        # Registry cleanup stays on the UI thread (SQLite is thread-affine, and
        # it's fast) so the tracker view is already correct in the repaint below.
        for jid in ids:
            try:
                self.registry.clear_status(jid)       # drop any tracker status too
                self.registry.clear_resume_path(jid)  # résumé link is stale either way
                self.registry.clear_tailor_failure(jid)  # deleted job needs no re-run flag
            except Exception:  # noqa: BLE001 - bookkeeping only
                pass
        # Optimistic: drop the rows from the in-memory frame and repaint now; the
        # multi-file CSV rewrite (the part that used to freeze the UI for seconds)
        # runs on the background write queue.
        if not self.df.empty:
            self.df = self.df[~self.df["job_posting_id"].astype(str).isin(set(ids))]
            self.df = self.df.reset_index(drop=True)
        for jid in ids:
            self.id_to_path.pop(jid, None)
        self._apply_df_views()
        self._set_status(f"Deleting {len(ids)} job(s) in background …")

        def work():
            n = jobsdata.delete_jobs(ids)
            trash_failed = []
            for jid in ids:
                try:
                    # Best-effort: refuses (False) anything outside the output root;
                    # a locked folder (open in Explorer) raises and is reported below.
                    recycle_resume_folder(folders.get(jid))
                except OSError:  # includes send2trash's TrashPermissionError
                    trash_failed.append(jid)
            return n, trash_failed

        self._enqueue_write(work, description=f"delete {len(ids)} job(s)",
                            on_done=self._finish_delete)

    def _finish_delete(self, result) -> None:
        n, trash_failed = result
        msg = f"Deleted {n} job(s)."
        if trash_failed:
            msg += (f" Couldn't move {len(trash_failed)} résumé folder(s) to the "
                    "Recycle Bin (folder in use?); remove them by hand.")
        self._set_status(msg)

    def _edit_manual_job(self, jid) -> None:
        """Field-fix a manually-added job (URL/title/company/JD) via the manual-add
        form in edit mode. Does not re-score/re-tailor — preserves the job's id and
        score so its tracker status and résumé link survive the edit."""
        jid = str(jid or "")
        if not jid:
            return
        row = jobsdata.master_row(jid) or {}
        initial = {
            "url": str(row.get("url", "") or ""),
            "title": str(row.get("job_title", "") or ""),
            "company": str(row.get("company_name", "") or ""),
            "jd_text": str(row.get("job_description_formatted", "")
                           or row.get("job_summary", "") or ""),
        }
        dlg = ManualAddDialog(self, edit_mode=True, initial=initial)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        record = dict(row)
        record["job_posting_id"] = jid            # identity is stable across an edit
        record["url"] = vals["url"]
        record["job_title"] = vals["title"]
        record["company_name"] = vals["company"]
        if vals["jd_text"]:
            record["job_description_formatted"] = vals["jd_text"]
            record["job_summary"] = vals["jd_text"][:1000]
        record.setdefault("source", "manual")
        record.setdefault("run_label", "manual")
        jobsdata.update_manual_job(record, old_id=jid)
        self.reload_data_async()
        self._set_status(f"Updated job: {vals['title']} @ {vals['company']}.")
