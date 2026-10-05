"""The dashboard's tailoring actions: opening a posting in the apply panel,
tailoring résumés for the selected jobs on a capped thread pool, writing a
cover letter for a tailored job, and the per-job chat. `_TailorActions`
is a base of `MainWindow`, and its methods run on a `MainWindow`.

Split out of `qt.main_window`.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6 import QtWidgets

import chrome_launch
import errmsg
import jobsdata
import osopen
import settings
from qt import workers
from qt.chat_dialog import JobChatDialog
from qt.plaintext import literal

# Tailoring is parallel (all selected at once). Above this many, warn first — a big
# fan-out means that many simultaneous Gemini calls + pdflatex processes (API limits /
# local load). Below it, just go.
PARALLEL_WARN_THRESHOLD = 5

# Cap on concurrently-running tailor jobs. Uncapped (a 14-job batch = 14
# threads, each making several Gemini calls at once) the batch stampedes the
# per-minute quota: every thread 429s together, retries together, and the
# unlucky tail exhausts its retries and fails. Four keeps the pipeline busy
# while staying under free-tier RPM limits; the rest of the batch queues.
MAX_PARALLEL_TAILORS = 4


def _tailor_pool_size(n_jobs: int) -> int:
    """Worker-thread count for a tailor batch of `n_jobs`."""
    return max(1, min(n_jobs, MAX_PARALLEL_TAILORS))


_JD_COLUMNS = ("job_description_formatted", "job_description")


def _with_master_jd(job: dict) -> dict:
    """`job` with its full description from the master CSV when the payload
    carries none. A hand-added job's dashboard row holds
    only the 1000-character `job_summary`: the gz bridge leaves its
    description out, and the master CSV keeps it. Worker threads only:
    `jobsdata.master_row` reads the file."""
    if any(str(job.get(col) or "").strip() for col in _JD_COLUMNS):
        return job
    jid = str(job.get("job_posting_id") or "").strip()
    try:
        row = jobsdata.master_row(jid) if jid else None
    except Exception:  # noqa: BLE001 - the payload as it is still tailors
        row = None
    if not row or not any(str(row.get(col) or "").strip() for col in _JD_COLUMNS):
        return job
    return {**job, **{col: str(row.get(col) or "") for col in _JD_COLUMNS}}


# How many warning lines one degraded job contributes to the batch dialog before the
# rest are summarised. A grounding gate having a bad day can produce one per bullet,
# and a message box that tall is a wall, not a report — the folder's tailor_report.txt
# is the complete copy.
MAX_TAILOR_WARNINGS_SHOWN = 5


def _tailor_warning_lines(rows: list[dict]) -> str:
    """The dialog block for jobs that finished WITH warnings: '  - <label>: <warning>'
    per warning, truncated per job so one noisy run can't bury the others.

    Paths are scrubbed here, at the screen: an advisory quotes the exception that
    skipped an optional artifact, and an OSError's own message carries the
    offending path, which names the person's home directory (see errmsg). The
    folder's tailor_report.txt keeps the full line."""
    out: list[str] = []
    for r in rows:
        label = r.get("label") or r.get("id") or "job"
        warns = list(r.get("warnings") or [])
        for w in warns[:MAX_TAILOR_WARNINGS_SHOWN]:
            out.append(f"  - {label}: {errmsg.scrub_paths(w)}")
        extra = len(warns) - MAX_TAILOR_WARNINGS_SHOWN
        if extra > 0:
            out.append(f"  - {label}: ...and {extra} more (see tailor_report.txt)")
    return "\n".join(out)


class _TailorActions:
    """`MainWindow`'s tailor actions (see the module docstring)."""

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
        # Read the toggle HERE, on the UI thread — settings.load() touches the disk and
        # must not run inside the worker (same shape as _generate_cover_for's tone read).
        open_url = settings.load().get("apply_open_browser", True) is not False
        self._applying = True
        self._set_status("Opening application …" if open_url
                         else "Building the apply sheet …")
        workers.run_async(self, lambda: self._apply_work(jid, payload, open_url),
                          on_done=self._finish_apply_ok, on_error=self._finish_apply_error)

    def _apply_work(self, jid: str, payload: dict | None, open_url: bool = True):
        """Build the apply context, and launch the posting only when `open_url`.

        `open_url` is resolved by the caller on the UI thread (the apply_open_browser
        setting); it defaults True so the CLI-ish callers and older tests keep today's
        behaviour. With it off the sheet still carries the posting URL, so nothing is
        lost — the browser just doesn't take over the screen."""
        from resume_tailor import apply as apply_mod
        if payload:
            # a hand-added job's full description, for an apply sheet the
            # resolver backfills (on the worker thread)
            payload = _with_master_jd(payload)
        folder = apply_mod.resolve_generated_dir(job_id=jid, job=payload)
        ctx = apply_mod.build_apply_context(folder)
        url = ctx.get("apply_url", "")
        if url and open_url:
            try:
                chrome_launch.open_in_chrome(url)
            except Exception:  # noqa: BLE001
                pass
        return ctx

    def _finish_apply_ok(self, ctx: dict) -> None:
        self._applying = False
        job = ctx.get("job") or {}
        self._apply_panel_job = job  # for the panel's "I applied to this job" button
        resume_pdf = ctx.get("resume_pdf", "")
        # Open the right-side Apply panel (copyable paths + the apply sheet) and hide
        # the bottom score preview while it's up — the ✕ on the panel restores it.
        self.apply_panel.show_application(ctx)
        self._apply_panel_open = True
        self.preview.setVisible(False)
        self._preview_shown = False
        self.apply_panel.show()
        sizes = self.hsplit.sizes()
        if len(sizes) >= 2 and sizes[-1] < 50:  # first open — carve out room for the panel
            total = sum(sizes) or 1000
            self.hsplit.setSizes([max(420, total - 380), 380])
        if resume_pdf:
            QtWidgets.QApplication.clipboard().setText(resume_pdf)
        self._set_status(f"Apply sheet ready for {job.get('company', '?')}: "
                         f"{job.get('title', '?')}. Paste it into Claude-in-Chrome; "
                         f"review before submitting.")

    def _close_apply_panel(self) -> None:
        self._apply_panel_open = False
        self.apply_panel.hide()
        self._apply_preview_visibility()  # restores the score preview on a job tab

    def _mark_applied_from_panel(self) -> None:
        """The panel's "I applied to this job" button: confirm, record the job as
        applied in the tracker (using the panel's stored marker identity, so it works
        even when the row isn't in the loaded data), mark it seen, and close the panel
        — so the one button doubles as "added to tracker" and "exit"."""
        job = getattr(self, "_apply_panel_job", None) or {}
        jid = str(job.get("job_posting_id") or "").strip()
        if not jid:
            self._set_status("Couldn't identify this job to add to the tracker.")
            return
        title, company = job.get("title", "this job"), job.get("company", "?")
        if QtWidgets.QMessageBox.question(
                self, "Mark as applied?",
                literal(f"Add '{title}' @ {company} to your application tracker as applied?")
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.registry.set_status(jid, "applied", company=company,
                                 job_title=title, url=job.get("url", ""))
        self._mark_ids_seen([jid])   # applied implies seen (matches the right-click path)
        self._close_apply_panel()
        self._set_status(f"Marked applied: added {company} to the tracker.")

    def _finish_apply_error(self, exc) -> None:
        self._applying = False
        msg = errmsg.for_user(exc)
        self._set_status(msg.splitlines()[0] if msg else "Apply failed")
        QtWidgets.QMessageBox.information(
            self, "Apply", literal(f"{msg}\n\nUse 'Tailor resume' on this job, then try Apply again."))

    # ---- tailor --------------------------------------------------------------

    def _confirm_large_tailor(self, n: int) -> bool:
        """Warn before fanning out a big parallel batch (separate method so it's
        trivially testable). Returns True to proceed."""
        return QtWidgets.QMessageBox.question(
            self, "Tailor many resumes at once?",
            literal(f"About to tailor {n} resumes ({MAX_PARALLEL_TAILORS} at a time), each "
            f"making its own Gemini calls and launching pdflatex. If the API rate-limits, "
            f"jobs wait it out and retry; any job that still fails turns red in the "
            f"Unseen tab for a re-run. Continue?")
        ) == QtWidgets.QMessageBox.StandardButton.Yes

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
        if len(jobs) > PARALLEL_WARN_THRESHOLD and not self._confirm_large_tailor(len(jobs)):
            return
        cfg = settings.load()
        cover = QtWidgets.QMessageBox.question(
            self, "Cover letter",
            literal(f"Also generate a cover letter for the selected {len(jobs)} job(s)?")
        ) == QtWidgets.QMessageBox.StandardButton.Yes
        opts = {"cover_letter": cover, "ats_report": bool(cfg.get("tailor_ats_report", True)),
                "prep_sheet": bool(cfg.get("tailor_prep_sheet", False)),
                "tone": cfg.get("resume_tone", "professional")}
        self._start_tailor(jobs, opts)

    def _start_tailor(self, jobs: list[dict], opts: dict, on_finished=None) -> bool:
        """Launch the tailor worker — the ONE shared path both the Tailor button
        (`_tailor_selected`) and auto-apply queueing (`_queue_for_auto_apply`)
        come through, so queue-chaining never duplicates the worker plumbing.

        Returns False WITHOUT launching when a run is already in flight (the
        `_tailoring` guard). `on_finished(results, exc)` — exactly one of the
        two is None — fires on the UI thread AFTER the standard `_finish_tailor`
        / `_finish_tailor_error` handling, so a chained step already sees the
        registry's resume paths recorded."""
        if getattr(self, "_tailoring", False):
            return False
        self._tailoring = True
        self._tailor_recorded = set()   # job ids bookkept by _on_tailor_job_done
        self.btn_tailor.setEnabled(False)
        self._apply_auth_env()
        plural = "resume" if len(jobs) == 1 else "resumes in parallel"
        self._set_status(f"Tailoring {len(jobs)} {plural} …")

        def done(results) -> None:
            self._finish_tailor(results)
            if on_finished is not None:
                on_finished(results, None)

        def error(exc: BaseException) -> None:
            self._finish_tailor_error(exc)
            if on_finished is not None:
                on_finished(None, exc)

        try:
            workers.run_async(self, lambda: self._tailor_work(jobs, opts),
                              on_done=done, on_error=error)
        except Exception as exc:  # noqa: BLE001 - launch (thread spawn) failed; clear
            self._tailoring = False  # the re-entry guard so Tailor isn't dead-locked
            self.btn_tailor.setEnabled(True)  # (same shape as _generate_cover_for's)
            if on_finished is not None:  # park queue-chained "tailoring" entries as
                on_finished(None, RuntimeError(  # failed — orphans are unclaimable
                    f"tailor launch failed: {errmsg.for_user(exc)}"))
            # Surface in the status bar instead of re-raising into the Qt event
            # loop, where the exception would just be printed and swallowed.
            self._set_status(f"Could not start tailoring: {errmsg.for_user(exc)}")
            return False
        return True

    def _tailor_work(self, jobs: list[dict], opts: dict) -> list[dict]:
        """Tailor every selected job CONCURRENTLY (all at once) on a thread pool, and
        return a per-job outcome list. No registry/SQLite writes happen here — those
        are done back on the UI thread in `_finish_tailor` (the registry connection is
        thread-affine and concurrent writes would contend). Per-job exceptions are
        captured so one failure never sinks the rest of the batch."""
        from resume_tailor import assets, llm
        from resume_tailor import tailor as tailor_resume

        # Pre-warm the shared lru_caches once so N threads don't each re-parse the YAML
        # / re-extract the example PDF (and so there's no cold-cache race). Best-effort:
        # tailor() will surface any real loading error per job.
        for warm in (assets.load_master, assets.atoms_by_id, assets.blocks,
                     assets.template_head, assets.example_text):
            try:
                warm()
            except Exception:  # noqa: BLE001 - pre-warm only
                pass
        llm.reset_usage()  # once for the whole batch; jobs pass reset_usage=False

        n = len(jobs)
        done_lock = threading.Lock()
        done = 0

        def report(label: str, msg: str) -> None:
            # Cross-thread-safe: queued onto the UI thread by the Qt signal.
            self.tailor_progress.emit(f"Tailoring ({done}/{n} done): {label}: {msg}")

        def one(job: dict) -> dict:
            nonlocal done
            label = f'{job.get("job_title") or "Role"} @ {job.get("company_name") or "?"}'
            # Degraded-run channel: a grounding-gate drop, an over-length PDF, or a
            # failed optional artifact. The engine also writes them to the folder's
            # tailor_report.txt; collecting them here is what lets _finish_tailor stop
            # reporting a half-worked job as a clean success. Appending from the pool
            # thread is safe: the list is this job's alone and is only read after the
            # call returns.
            warnings: list[str] = []
            try:
                # a hand-added job's payload holds only its summary: the full
                # description comes from the master here, on the worker, for
                # Tailor resume and the auto-apply queue's tailor alike
                job = _with_master_jd(job)
                out = tailor_resume(job, cover_letter=opts["cover_letter"],
                                    ats_report=opts["ats_report"], prep_sheet=opts["prep_sheet"],
                                    tone=opts["tone"], reset_usage=False,
                                    on_status=lambda m, lbl=label: report(lbl, m),
                                    on_warning=warnings.append)
                result = {"id": job.get("job_posting_id"), "label": label,
                          "dir": out, "error": None, "warnings": warnings}
            except Exception as exc:  # noqa: BLE001 - capture per-job; report in the summary
                result = {"id": job.get("job_posting_id"), "label": label,
                          "dir": None, "error": errmsg.for_user(exc), "warnings": warnings}
            with done_lock:
                done += 1
            # Queued to the UI thread: the registry records this job NOW, so an
            # interrupted batch keeps everything already finished.
            self.tailor_job_done.emit(result)
            self.tailor_progress.emit(f"Tailoring ({done}/{n} done): {label} finished")
            return result

        with ThreadPoolExecutor(max_workers=_tailor_pool_size(n)) as pool:
            return list(pool.map(one, jobs))

    def _record_tailor_result(self, result: dict) -> bool:
        """Write ONE job's outcome to the registry (UI thread only): success
        records the resume folder (clearing any old red flag), failure records
        the red 'tailor failed — re-run' flag the unseen tab shows. True when
        the write landed."""
        jid = (result or {}).get("id")
        if not jid:
            return False
        try:
            if result.get("dir"):
                self.registry.record_resume(jid, str(result["dir"]))
            else:
                self.registry.record_tailor_failure(
                    jid, str(result.get("error") or "unknown error"))
        except Exception:  # noqa: BLE001 - bookkeeping only; the view heals on reload
            return False
        return True

    def _on_tailor_job_done(self, result: dict) -> None:
        """Bookkeep ONE finished tailor job (queued here, the UI thread, from a
        pool thread the moment the job ends). Incremental on purpose — a batch
        interrupted at job 12 of 14 must keep those 12 results; the July 8 crash
        lost all of them because bookkeeping waited for the whole batch."""
        if self._record_tailor_result(result):
            rec = getattr(self, "_tailor_recorded", None)
            if rec is not None:
                rec.add(result["id"])

    def _finish_tailor(self, results: list[dict]) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        results = results or []
        oks = [r for r in results if r.get("dir")]
        fails = [r for r in results if not r.get("dir")]
        # Normally every result was already bookkept per job (the queued
        # _on_tailor_job_done deliveries precede this callback — same FIFO
        # event queue). Catch up on any that weren't, e.g. a registry hiccup
        # mid-batch or a test driving this callback directly.
        recorded = getattr(self, "_tailor_recorded", set())
        for r in results:
            if r.get("id") and r["id"] not in recorded:
                self._record_tailor_result(r)
        total = len(results)
        # A job that produced a PDF but warned on the way (a two-page résumé, a
        # grounding-gate drop, a skipped ATS report) is still a SUCCESS — the registry
        # contract stays binary and it keeps its recorded resume folder. It just no
        # longer gets reported as a clean one. Full detail lives in the folder's
        # tailor_report.txt; the dialog carries enough to know to go look.
        degraded = [r for r in oks if r.get("warnings")]
        if fails:
            lines = "\n".join(
                f"  - {r.get('label') or r.get('id') or 'job'}: "
                f"{r.get('error') or 'unknown error'}" for r in fails)
            text = f"Tailored {len(oks)} of {total} resume(s).\n\nFailed:\n{lines}"
            if degraded:
                text += f"\n\nFinished with warnings:\n{_tailor_warning_lines(degraded)}"
            QtWidgets.QMessageBox.warning(self, "Tailor resume", literal(text))
            status = f"Tailored {len(oks)} of {total}; {len(fails)} failed (see dialog)."
            if degraded:
                status += f" {len(degraded)} with warnings."
            self._set_status(status)
        elif degraded:
            QtWidgets.QMessageBox.warning(
                self, "Tailor resume",
                literal(f"Tailored {len(oks)} of {total} resume(s), {len(degraded)} with "
                f"warnings:\n\n{_tailor_warning_lines(degraded)}\n\n"
                f"Each folder's tailor_report.txt has the full record."))
            self._set_status(
                f"Resume(s) ready ({len(oks)}); {len(degraded)} with warnings (see dialog).")
        elif oks:
            self._set_status(f"Resume(s) ready ({len(oks)}).")
        last = oks[-1]["dir"] if oks else None
        # Only open the file manager when the user opted in — off by default so a
        # multi-job batch doesn't spawn a window per résumé (Settings → Dashboard).
        if last and settings.load().get("tailor_open_folder", False):
            try:
                osopen.open_path(last)
            except OSError:
                pass
        self.reload_data_async()

    def _finish_tailor_error(self, exc) -> None:
        self._tailoring = False
        self.btn_tailor.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "Tailor resume", literal(f"Tailoring failed: {errmsg.for_user(exc)}"))
        self._set_status(f"Tailor failed: {errmsg.for_user(exc)}")

    # ---- cover letter for an already-tailored job (right-click) ---------------

    def _cover_state(self, jid: str) -> str | None:
        """Drives the right-click menu item: None when the job isn't tailored
        (no folder / resume PDF / apply.md — same readiness as Apply), else
        "exists"/"missing" by whether the cover-letter PDF is on disk."""
        ready, folder = self._apply_ready(jid)
        if not ready or folder is None:
            return None
        from resume_tailor import output
        try:
            return "exists" if (folder / output.cover_filename()).exists() else "missing"
        except OSError:
            return None

    def _generate_cover_for(self, jid: str) -> None:
        if getattr(self, "_covering", False):
            return
        state = self._cover_state(jid)
        if state is None:
            self._set_status("Tailor this job first; the cover letter reuses its "
                             "tailored résumé bullets.")
            return
        if state == "exists" and QtWidgets.QMessageBox.question(
                self, "Regenerate cover letter?",
                "A cover letter already exists for this job. Regenerate and "
                "replace it?") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        payload = self._payload_with_master_fallback(jid)
        if not payload:
            self._set_status("Job description not available; cannot generate a "
                             "cover letter.")
            return
        _, folder = self._apply_ready(jid)
        tone = settings.load().get("resume_tone", "professional")
        self._covering = True
        self._apply_auth_env()
        self._set_status(f"Generating cover letter for {payload['company_name']}: "
                         f"{payload['job_title']} …")
        try:
            workers.run_async(self, lambda: self._cover_work(payload, folder, tone),
                              on_done=self._finish_cover, on_error=self._finish_cover_error)
        except Exception:  # noqa: BLE001 - launch (thread spawn) failed; clear the
            self._covering = False  # re-entry guard so the menu item isn't dead-locked
            raise

    def _cover_work(self, job: dict, folder, tone: str):
        from resume_tailor.run import generate_cover_letter
        # a hand-added job's payload holds only its summary: the full
        # description comes from the master here, on the worker
        job = _with_master_jd(job)
        # Re-check on the worker: the folder may have been deleted between the
        # menu click and this thread starting.
        if not folder or not Path(folder).is_dir():
            raise RuntimeError("The tailored folder no longer exists; re-tailor "
                               "this job first.")
        return generate_cover_letter(job, Path(folder), tone=tone,
                                     on_status=self.tailor_progress.emit)

    def _finish_cover(self, path) -> None:
        self._covering = False
        self._set_status(f"Cover letter ready → {path}")

    def _finish_cover_error(self, exc) -> None:
        self._covering = False
        QtWidgets.QMessageBox.warning(self, "Cover letter",
                                      literal(f"Cover letter failed: {errmsg.for_user(exc)}"))
        self._set_status(f"Cover letter failed: {errmsg.for_user(exc)}")

    def _payload_with_master_fallback(self, jid: str) -> dict | None:
        """The job's row payload, rebuilt from the master CSV when the row isn't
        in the loaded frames (e.g. a tracker-only job) — the same fallback the
        edit dialog uses. None when the id is unknown everywhere."""
        payload = self._job_payload(jid)
        if payload is not None:
            return payload
        row = jobsdata.master_row(jid) or {}
        if not row:
            return None
        return {
            "job_posting_id": jid,
            "company_name": str(row.get("company_name", "") or ""),
            "job_title": str(row.get("job_title", "") or ""),
            "job_description_formatted": str(row.get("job_description_formatted", "") or ""),
            "job_description": str(row.get("job_description", "") or ""),
            "job_summary": str(row.get("job_summary", "") or ""),
            "url": str(row.get("url", "") or ""),
        }

    # ---- per-job chat --------------------------------------------------------

    def _ask_ai_for(self, jid: str) -> None:
        """Open (or raise) the chat window for one job.

        Deliberately never refuses: an untailored job still has its JD and the
        master file to answer from, and a job the loaded frames have never heard
        of may still have a tailored folder on disk for the resolver to find. So
        this degrades to a thinner context instead of erroring.
        """
        jid = str(jid or "").strip()
        if not jid:
            return
        open_dlg = self._chat_dialogs.get(jid)
        if open_dlg is not None:      # one window per job; a second click raises it
            open_dlg.show()
            open_dlg.raise_()
            open_dlg.activateWindow()
            return
        payload = self._payload_with_master_fallback(jid) or {"job_posting_id": jid}
        self._apply_auth_env()        # the chat calls the engine, same as tailor/cover
        # Parented to this window and self-removing on close — see qt/chat_dialog.py.
        # `prepare` runs on the chat's worker: a hand-added job's full
        # description from the master
        dlg = JobChatDialog(payload, parent=self,
                            on_closed=lambda: self._chat_dialogs.pop(jid, None),
                            prepare=_with_master_jd)
        self._chat_dialogs[jid] = dlg
        dlg.show()
        title = payload.get("job_title") or payload.get("title") or "this job"
        company = payload.get("company_name") or payload.get("company") or "?"
        self._set_status(f"Ask AI: {title} @ {company}. Answers come only from "
                         f"this job's apply sheet and posting.")

    def _ask_ai_from_panel(self) -> None:
        """The Apply panel's Ask AI button: chat about the job it is showing."""
        job = getattr(self, "_apply_panel_job", None) or {}
        jid = str(job.get("job_posting_id") or "").strip()
        if not jid:
            self._set_status("Couldn't identify this job to ask about.")
            return
        self._ask_ai_for(jid)
