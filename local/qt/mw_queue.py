"""The dashboard's auto-apply queue actions: writing the queue, queueing the
selected jobs (tailoring the ones that need it first), marking a queued
job applied or seen, and saving an ATS password. `_QueueActions` is a
base of `MainWindow`, and its methods run on a `MainWindow`.

Split out of `qt.main_window`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6 import QtWidgets

import apply_queue
import ats_accounts
import errmsg
import jobsdata
import settings
from qt.plaintext import literal


class _QueueActions:
    """`MainWindow`'s queue actions (see the module docstring)."""

    # ---- batch auto-apply queueing ---------------------------------------

    def _submit_queue_write(self, fn, on_done=None, on_error=None) -> None:
        """Run an apply-queue mutation on the background write queue.

        Deliberately NOT `_enqueue_write` — its self-write suppression exists
        for the source-CSV watcher, while the queue file has its own watcher
        inside ApplyQueuePanel that SHOULD see these writes land. Late-binds
        `self._writes` so tests that swap in an inline runner drive it too."""
        self._writes.submit(
            fn, on_done=on_done,
            on_error=on_error or (lambda exc: self._set_status(
                f"Apply-queue write failed: {errmsg.for_user(exc)}")))

    def _queue_artifacts(self, folder) -> dict:
        """The artifact paths a queue entry carries for a tailored folder.
        Optional files not on disk map to "" so the agent never chases a path
        that doesn't exist."""
        from resume_tailor import output
        folder = Path(folder)

        def existing(name: str) -> str:
            p = folder / name
            try:
                return str(p) if p.exists() else ""
            except OSError:
                return ""

        return {
            "folder": str(folder),
            "resume_pdf": existing(output.resume_filename()),
            "cover_letter_pdf": existing(output.cover_filename()),
            "apply_md": existing("apply.md"),
        }

    def _queue_entry_for(self, jid: str, batch_id: str, status: str) -> dict | None:
        """A fresh apply-queue entry for one job, from the loaded frame — with
        the tracker-row / master-CSV fallback for ids the frames don't carry
        (tracker-only jobs; the same fallback _generate_cover_for uses).
        Returns None when no apply URL can be resolved ANYWHERE: an entry with
        an empty apply_url would send the apply agent nowhere and burn one of
        the job's attempts, so it must never reach the queue."""
        row = self._row_for(jid)
        company = self._cell(row, "company_name")
        title = self._cell(row, "job_title")
        url = self._url_by_id.get(jid) or self._cell(row, "url")
        easy_raw = self._cell(row, "is_easy_apply")
        if not (company and title and url):
            tracked = self._tracked.get(jid) or {}
            company = company or str(tracked.get("company") or "")
            title = title or str(tracked.get("job_title") or "")
            url = url or str(tracked.get("url") or "")
        if not (company and title and url):
            master = jobsdata.master_row(jid) or {}
            company = company or str(master.get("company_name") or "")
            title = title or str(master.get("job_title") or "")
            url = url or str(master.get("url") or "")
            easy_raw = easy_raw or str(master.get("is_easy_apply") or "")
        if not url.strip():
            return None
        easy = easy_raw.strip().lower() in ("true", "1", "yes")
        return apply_queue.new_entry(
            jid,
            company=company,
            title=title,
            apply_url=url,
            is_easy_apply=easy,
            batch_id=batch_id,
            status=status)

    def _queue_apply_selected(self) -> None:
        """The action-bar 'Queue auto-apply' button: queue the selection."""
        ids = self._selected_ids()
        if not ids:
            self._set_status("Select one or more jobs to queue for auto-apply.")
            return
        self._queue_for_auto_apply(ids)

    def _queue_for_auto_apply(self, ids) -> None:
        """'Queue for auto-apply' (context menu + action bar): skip jobs already
        applied to, enforce the batch cap, enqueue apply-ready jobs with their
        artifact paths, and offer ONE tailor-then-queue run for the rest (cover
        letter included — that Gemini spend is consented by the Yes click)."""
        seen: set[str] = set()
        ids = [s for s in (str(i).strip() for i in (ids or []))
               if s and not (s in seen or seen.add(s))]
        if not ids:
            self._set_status("Select one or more jobs to queue for auto-apply.")
            return
        try:
            applied = {str(r.get("job_posting_id")) for r in self.registry.status_rows()
                       if r.get("status") == "applied"}
        except Exception:  # noqa: BLE001 - registry hiccup: skip nothing, queue on
            applied = set()
        notes: list[str] = []
        skipped = [i for i in ids if i in applied]
        if skipped:
            notes.append(f"skipped {len(skipped)} already-applied")
        remaining = [i for i in ids if i not in applied]
        if not remaining:
            self._set_status(f"Nothing to queue: skipped {len(skipped)} "
                             "already-applied job(s).")
            return
        cfg = settings.load()
        try:
            cap = int(cfg.get("auto_apply_batch_cap", 10) or 10)
        except (TypeError, ValueError):
            cap = 10
        if len(remaining) > cap:
            notes.append(f"capped at {cap} (auto_apply_batch_cap)")
            remaining = remaining[:cap]

        ready: list[tuple[str, Path]] = []
        not_ready: list[str] = []
        for jid in remaining:
            ok, folder = self._apply_ready(jid)
            if ok and folder is not None:
                ready.append((jid, folder))
            else:
                not_ready.append(jid)

        batch_id = datetime.now().strftime("batch-%Y%m%d-%H%M%S")
        queued_n = 0
        no_data = 0
        for jid, folder in ready:
            entry = self._queue_entry_for(jid, batch_id, "queued")
            if entry is None:      # no apply URL anywhere — refuse (see _queue_entry_for)
                no_data += 1
                continue
            entry["artifacts"].update(self._queue_artifacts(folder))
            self._submit_queue_write(lambda e=entry: apply_queue.enqueue(e))
            queued_n += 1
        if no_data:
            notes.append(f"{no_data} without job data, not queued")

        started_tailor = False
        tailor_launch_failed = False
        if not_ready:
            if getattr(self, "_tailoring", False):
                notes.append(f"{len(not_ready)} not tailored, skipped while a "
                             "tailor run is in flight")
            elif QtWidgets.QMessageBox.question(
                    self, "Queue for auto-apply",
                    literal(f"{len(not_ready)} job(s) aren't tailored yet. Tailor now "
                    "(cover letter included; spends Gemini credit) and queue "
                    "when done?")) == QtWidgets.QMessageBox.StandardButton.Yes:
                jobs = [j for j in (self._job_payload(i) for i in not_ready) if j]
                # A job whose entry can't resolve an apply URL can never be
                # queued after tailoring either — drop it BEFORE spending
                # Gemini credit on it, and count it with the data-less ones.
                entries: list[dict] = []
                with_data: list[dict] = []
                for j in jobs:
                    entry = self._queue_entry_for(
                        str(j["job_posting_id"]), batch_id, "tailoring")
                    if entry is not None:
                        entries.append(entry)
                        with_data.append(j)
                jobs = with_data
                missing = len(not_ready) - len(jobs)
                if missing:
                    notes.append(f"{missing} without job data, not queued")
                if jobs:
                    # Enqueue as "tailoring" FIRST so the panel shows them the
                    # moment the worker starts; _finish_queue_tailor flips each
                    # to "queued" (set_artifacts) or parks it "failed".
                    for entry in entries:
                        self._submit_queue_write(lambda e=entry: apply_queue.enqueue(e))
                    opts = {"cover_letter": True,
                            "ats_report": bool(cfg.get("tailor_ats_report", True)),
                            "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                            "tone": cfg.get("resume_tone", "professional")}
                    self._queue_tailor_pending = [str(j["job_posting_id"])
                                                  for j in jobs]
                    started_tailor = self._start_tailor(
                        jobs, opts, on_finished=self._finish_queue_tailor)
                    if not started_tailor:  # raced the guard / spawn failed:
                        tailor_launch_failed = True  # keep _start_tailor's error
                        self._finish_queue_tailor(None, RuntimeError(  # park honestly
                            "a tailor run was already in flight"))     # (no-op if already parked)
            else:
                notes.append(f"{len(not_ready)} not tailored, left out")

        if not started_tailor and not tailor_launch_failed:
            # otherwise _start_tailor owns the status line
            parts = ([f"Queued {queued_n} job(s) for auto-apply"] if queued_n else [])
            parts += notes
            self._set_status((" · ".join(parts) + ".") if parts
                             else "Nothing queued for auto-apply.")

    def _finish_queue_tailor(self, results, exc=None) -> None:
        """After a queue-chained tailor run (fires on the UI thread, AFTER the
        standard _finish_tailor handling): success flips each entry
        tailoring -> queued with its artifact paths (apply_queue.set_artifacts);
        failure parks it `failed` with the reason in its notes."""
        pending = [str(i) for i in getattr(self, "_queue_tailor_pending", []) or []]
        self._queue_tailor_pending = []

        def park_failed(jid: str, note: str) -> None:
            self._submit_queue_write(
                lambda: apply_queue.finish(jid, "failed", notes=note))

        if exc is not None:
            for jid in pending:
                park_failed(jid, f"tailor failed: {errmsg.for_user(exc)}")
            return
        by_id = {str(r.get("id") or ""): r for r in (results or [])}
        for jid in pending:
            r = by_id.get(jid)
            if r is None:
                park_failed(jid, "tailor returned no result for this job")
            elif r.get("dir"):
                arts = self._queue_artifacts(r["dir"])
                self._submit_queue_write(
                    lambda jid=jid, arts=arts: apply_queue.set_artifacts(jid, arts))
            else:
                park_failed(jid, f"tailor failed: {r.get('error') or 'unknown error'}")

    def _apply_queue_mark_applied(self, entry: dict) -> None:
        """Auto-apply panel action: record a queued job as applied in the tracker,
        mark it seen (applied implies seen — matches _mark_applied_from_panel), and
        drop it from the queue. Queue write rides _submit_queue_write."""
        jid = str(entry.get("job_posting_id") or "").strip()
        if not jid:
            return
        self.registry.set_status(jid, "applied",
                                 company=entry.get("company", ""),
                                 job_title=entry.get("title", ""),
                                 url=entry.get("apply_url", ""))
        self._mark_ids_seen([jid])
        self._submit_queue_write(
            lambda: apply_queue.remove(jid),
            on_done=lambda _r: self.apply_queue_panel.refresh())
        self._refresh_tracker()

    def _apply_queue_mark_seen(self, entry: dict) -> None:
        """Auto-apply panel action: mark a queued job seen (no status → it stays
        under All Jobs) and drop it from the queue. The job may already be gone."""
        jid = str(entry.get("job_posting_id") or "").strip()
        if not jid:
            return
        self._mark_ids_seen([jid])

        def _remove() -> None:
            try:
                apply_queue.remove(jid)
            except apply_queue.UnknownJobError:
                pass

        self._submit_queue_write(
            _remove, on_done=lambda _r: self.apply_queue_panel.refresh())

    def _set_ats_password(self) -> None:
        """Store the ONE master ATS password (typed twice, password-echo) in the
        Windows Credential Manager via ats_accounts. The value goes straight
        from the dialog into keyring — never to disk, logs, or the queue."""
        echo = QtWidgets.QLineEdit.EchoMode.Password
        first, ok = QtWidgets.QInputDialog.getText(
            self, "Master ATS password", "New master password:", echo)
        if not ok:
            return
        second, ok = QtWidgets.QInputDialog.getText(
            self, "Master ATS password", "Repeat to confirm:", echo)
        if not ok:
            return
        if not first.strip() or first != second:
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                "The two entries were blank or didn't match; nothing was stored.")
            return
        try:
            stored = ats_accounts.set_master_password(first)
        except Exception as exc:  # noqa: BLE001 - keyring backend failure
            # The exception CLASS name only, deliberately -- the same rule
            # ats_accounts applies to its own secret-touching verbs
            # (_SECRET_VERBS), for the reason it gives there: str(e) out of a
            # keyring or clipboard backend could carry the password itself. This
            # dialog is the path the user actually takes, so it is the one that
            # has to hold the rule; the CLI held it and the GUI did not.
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                literal(f"Could not store the password ({type(exc).__name__}). Check that "
                f"the keyring package is installed and the Windows Credential "
                f"Manager is available."))
            return
        if not stored:
            QtWidgets.QMessageBox.warning(
                self, "Master ATS password",
                "Could not store the password (is the keyring package installed?).")
            return
        self._set_status("Master ATS password stored in Windows Credential Manager.")
        panel = getattr(self, "apply_queue_panel", None)
        if panel is not None:
            panel.refresh_password_state()
