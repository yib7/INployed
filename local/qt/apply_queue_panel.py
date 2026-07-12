"""The "Auto-apply" tab: a live, read-only mirror of the batch auto-apply queue.

The queue file itself (SP2's local/apply_queue.py) is the single source of
truth shared with the agent CLI; this panel only *displays* it and offers the
few human controls around it — Re-queue / Remove / Clear finished, opening a
job's artifacts, the master-password state, and "Copy kickoff command" (the
exact PowerShell line that starts the SP4 drain session).

Freshness: reads are lock-free (`apply_queue.load`, never quarantine=True — the
panel must never rename a file a locked writer owns). A QFileSystemWatcher
watches the queue file AND its directory, re-armed after every event because
the atomic os.replace that lands each mutation drops the file watch (the same
`_rearm_watcher` trick main_window.py uses for the CSV sources); a 500 ms
debounce coalesces bursts, and a 5 s mtime poll catches setups that emit no fs
events at all.

Mutations never run on the UI thread here: they go through the injected
`submit_write(fn, on_done=None, on_error=None)` callable. The main window
routes that to its SerialTaskQueue (`self._writes`) — the PLAN's concurrency
rule for dashboard queue writes — while standalone/tests fall back to a
synchronous inline runner.
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6 import QtCore, QtWidgets

import apply_queue
import ats_accounts
import osopen

# The repo root (this file lives in <root>/local/qt/) — the kickoff command
# cd's here so `claude` picks up the repo's .claude/skills/auto-apply skill.
REPO_ROOT = Path(__file__).resolve().parents[2]

# SP4's skill mirrors this prompt verbatim — keep the two in sync.
KICKOFF_PROMPT = "Use the auto-apply skill: drain the apply queue"

# PowerShell-safe (5.1: `;` chains, no `&&`; quoted path survives spaces).
# --model sonnet: runs the ORCHESTRATOR (this drain session) on Sonnet. The orchestrator only
#   delegates — claim a job, dispatch a per-job subagent, collect its one-paragraph report, run
#   the watchdogs; it never browses the web or pastes the master password itself (the per-job
#   subagents do), so its prompt-injection surface is low and Sonnet is sufficient for the
#   coordination while costing far less than Opus. The per-job subagents are independently
#   pinned to claude-sonnet-5 by the skill (SKILL.md DRAIN LOOP step 3); this flag governs only
#   the orchestrator — change the subagent model in the skill, not here.
# --dangerously-skip-permissions: runs UNATTENDED — Claude Code never stops to ask the
#   user to approve each browser/file/CLI action. The skill's own safety rails (never
#   submit, park at review, CAPTCHA/SSN/payment stop, per-job domain allowlist,
#   secret-safe master-password paste) live in the skill logic and stay in force.
KICKOFF_COMMAND = (
    f'cd "{REPO_ROOT}"; claude --model sonnet --dangerously-skip-permissions '
    f'"{KICKOFF_PROMPT}"'
)

# Safer alternative (the Start button's "Scoped" choice): same Sonnet drain, but instead
# of bypassing ALL permission checks it pre-approves ONLY the tools the drain uses —
# Bash scoped to `python …` (the two project CLIs), file read/write for the record,
# Task to dispatch per-job subagents, and the browser MCP. Anything else (rm, curl, a
# different MCP) still prompts, so the blast radius is far smaller than a blanket
# bypass — at the cost of an occasional pause if the agent reaches outside the list.
# The prompt is placed FIRST so the variadic --allowedTools can't swallow it.
KICKOFF_COMMAND_SCOPED = (
    f'cd "{REPO_ROOT}"; claude "{KICKOFF_PROMPT}" --model sonnet --allowedTools '
    f'Read Glob Grep Write Edit Task "Bash(python:*)" "mcp__claude-in-chrome__*"'
)


def _kickoff_argv(scoped: bool = False) -> list[str]:
    """The argv that opens a NEW PowerShell console running the drain command.

    Pure and testable — no subprocess call here. `scoped=False` launches the
    unattended KICKOFF_COMMAND (--dangerously-skip-permissions); `scoped=True`
    launches KICKOFF_COMMAND_SCOPED (--allowedTools, which pauses on anything
    outside the list). Both already embed KICKOFF_PROMPT and the quoted
    REPO_ROOT and contain their own double quotes (`cd "<root>"` and
    `"<prompt>"`); base64 via -EncodedCommand sidesteps PowerShell 5.1's quoting
    rules entirely (no re-tokenizing, no escaping) and round-trips cleanly for
    the test to decode.
    """
    command = KICKOFF_COMMAND_SCOPED if scoped else KICKOFF_COMMAND
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoExit", "-EncodedCommand", encoded]


def _spawn_kickoff(scoped: bool = False) -> None:
    """Default on_start_run: launch the drain in a brand-new, visible console.
    `scoped` selects the safer allowlisted variant over the blanket bypass.

    The flag is guarded via getattr so importing this module on a non-Windows
    box (CI, a dev's Mac) never raises at import time.
    """
    subprocess.Popen(
        _kickoff_argv(scoped),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


COLUMNS = ("Company", "Title", "Status", "Attempts", "Missing", "Updated", "Note")

_DEBOUNCE_MS = 500     # coalesce a burst of fs events into one refresh
_POLL_MS = 5000        # mtime-poll fallback when no fs events arrive


def _default_password_exists() -> bool:
    """Panel seam for the master-password state (module-level so tests patch it
    without ever querying the real Windows Credential Manager)."""
    return ats_accounts.password_exists()


def _run_inline(fn: Callable[[], Any],
                on_done: Optional[Callable[[Any], None]] = None,
                on_error: Optional[Callable[[BaseException], None]] = None) -> None:
    """Default submit_write: synchronous, for standalone use and tests. The
    main window injects a SerialTaskQueue-backed callable instead."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - surfaced via on_error, never raised into Qt
        if on_error is not None:
            on_error(exc)
        return
    if on_done is not None:
        on_done(result)


class ApplyQueuePanel(QtWidgets.QWidget):
    """Header (counts / password state / Start auto-apply run) + queue table +
    details pane + the Re-queue / Remove / Clear finished / Open buttons."""

    def __init__(self, queue_path: Path | str | None = None, *,
                 submit_write: Callable | None = None,
                 on_set_password: Callable[[], None] | None = None,
                 password_exists: Callable[[], bool] | None = None,
                 on_start_run: Callable[[], None] | None = None,
                 on_mark_applied: Callable[[Dict[str, Any]], None] | None = None,
                 on_mark_seen: Callable[[Dict[str, Any]], None] | None = None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue_override = Path(queue_path) if queue_path else None
        self._submit_write = submit_write or _run_inline
        self._on_set_password = on_set_password or (lambda: None)
        # Late-bound default so a monkeypatched module seam takes effect.
        self._password_exists = password_exists or (lambda: _default_password_exists())
        self._on_start_run = on_start_run or _spawn_kickoff
        self._on_mark_applied = on_mark_applied or (lambda _e: None)
        self._on_mark_seen = on_mark_seen or (lambda _e: None)
        self._jobs: List[Dict[str, Any]] = []
        self._mtime_sig: tuple | None = None
        self._build()
        self._setup_watcher()
        self.refresh()

    # ---- paths -----------------------------------------------------------------

    def _queue_file(self) -> Path:
        """Resolved at call time (explicit override > APPLY_QUEUE_PATH env >
        appdata default) so tests and env changes take effect immediately."""
        return apply_queue.queue_path(self._queue_override)

    # ---- construction ------------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        header = QtWidgets.QHBoxLayout()
        self.counts_label = QtWidgets.QLabel("")
        self.counts_label.setProperty("muted", True)
        header.addWidget(self.counts_label)
        header.addStretch(1)
        self.start_run_btn = QtWidgets.QPushButton("Start auto-apply run")
        self.start_run_btn.setProperty("accent", True)
        self.start_run_btn.setToolTip(
            "Launch an auto-apply drain in a NEW terminal window — click once, "
            "walk away. You pick how it runs: unattended (no approval prompts) "
            "or scoped (safer — pre-approves only the tools it needs). Works "
            "through up to batch_cap queued jobs; every application is PARKED at "
            "its review page for your approval — nothing is ever submitted.")
        self.start_run_btn.clicked.connect(self._start_run)
        header.addWidget(self.start_run_btn)
        self.pw_label = QtWidgets.QLabel("")
        header.addWidget(self.pw_label)
        self.pw_btn = QtWidgets.QPushButton("Set…")
        self.pw_btn.setToolTip(
            "Store the ONE master password every auto-created ATS account uses "
            "(Windows Credential Manager — never written to any file)")
        self.pw_btn.clicked.connect(lambda: self._on_set_password())
        header.addWidget(self.pw_btn)
        self.copy_pw_btn = QtWidgets.QPushButton("Copy password")
        self.copy_pw_btn.setToolTip(
            "Copy the master password to the clipboard for a manual login — it is "
            "never shown on screen. Clears from the clipboard when you click Clear.")
        self.copy_pw_btn.clicked.connect(self._copy_password)
        header.addWidget(self.copy_pw_btn)
        self.clear_pw_btn = QtWidgets.QPushButton("Clear")
        self.clear_pw_btn.setToolTip("Clear the master password from the clipboard.")
        self.clear_pw_btn.clicked.connect(self._clear_password)
        header.addWidget(self.clear_pw_btn)
        v.addLayout(header)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        for i, w in enumerate((150, 220, 110, 70, 60, 150, 200)):
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._update_details)
        v.addWidget(self.table, 1)

        details_label = QtWidgets.QLabel("Details")
        details_label.setProperty("muted", True)
        v.addWidget(details_label)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(160)
        v.addWidget(self.details)

        btns = QtWidgets.QHBoxLayout()

        def button(text, slot, tip=""):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            if tip:
                b.setToolTip(tip)
            btns.addWidget(b)
            return b

        self.requeue_btn = button(
            "Re-queue", self._requeue,
            "Send the selected job back to 'queued' (clears its missing answers "
            "and refreshes its apply.md standard answers)")
        self.remove_btn = button("Remove", self._remove,
                                 "Delete the selected entry from the queue")
        self.mark_applied_btn = button(
            "Mark applied", self._mark_applied,
            "Move to the Tracker as applied and remove from the queue")
        self.dont_apply_btn = button(
            "Don't apply", self._dont_apply,
            "Mark seen (keeps it in All Jobs) and remove from the queue")
        self.clear_btn = button(
            "Clear finished", self._clear_finished,
            "Drop every ready_to_submit / submitted / needs_human / failed entry")
        self.open_folder_btn = button("Open job folder", self._open_folder)
        self.open_record_btn = button("Open application record", self._open_record)
        btns.addStretch(1)
        v.addLayout(btns)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setProperty("muted", True)
        v.addWidget(self.status_label)

    # ---- live refresh (watcher + debounce + poll) ---------------------------------

    def _setup_watcher(self) -> None:
        self._watcher = QtCore.QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_fs_event)
        self._watcher.directoryChanged.connect(self._on_fs_event)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(_POLL_MS)
        self._poll.timeout.connect(self._poll_for_changes)
        self._poll.start()
        self._rearm_watcher()

    def _rearm_watcher(self) -> None:
        """Re-point the watcher at the queue file + its dir and re-snapshot the
        mtime signature. Called after EVERY event and refresh — the atomic
        os.replace each mutation lands with drops the old file watch."""
        w = self._watcher
        if w.files():
            w.removePaths(w.files())
        if w.directories():
            w.removePaths(w.directories())
        qp = self._queue_file()
        paths = [str(p) for p in (qp, qp.parent) if p.exists()]
        if paths:
            w.addPaths(paths)
        self._mtime_sig = self._current_sig()

    def _current_sig(self) -> tuple | None:
        try:
            st = os.stat(self._queue_file())
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _on_fs_event(self, _path: str) -> None:
        self._rearm_watcher()      # re-add the watch the replace just dropped
        self._debounce.start()     # coalesce the burst; refresh once it settles

    def _poll_for_changes(self) -> None:
        if self._current_sig() != self._mtime_sig:
            self.refresh()

    # ---- data ---------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the queue file (lock-free — never quarantine from a reader)
        and repaint the table, counts, and password state."""
        # Snapshot the poll baseline BEFORE the read: a write landing while we
        # load would otherwise hide in the load→snapshot window and (on a
        # no-fs-events mount) stay invisible until some LATER write moved the
        # sig again. Worst case with the pre-read baseline is one spare refresh.
        sig = self._current_sig()
        data = apply_queue.load(self._queue_file())
        self._jobs = [e for e in data.get("jobs", []) if isinstance(e, dict)]
        self._fill_table()
        self._update_counts()
        self._update_details()
        self.refresh_password_state()
        self._rearm_watcher()
        self._mtime_sig = sig   # override _rearm_watcher's post-read snapshot

    def _fill_table(self) -> None:
        selected = self._selected_job_id()
        table = self.table
        table.blockSignals(True)
        try:
            table.setRowCount(len(self._jobs))
            reselect = None
            for r, e in enumerate(self._jobs):
                arts = e.get("artifacts") or {}
                missing = e.get("missing_answers") or []
                cells = (
                    str(e.get("company", "")),
                    str(e.get("title", "")),
                    str(e.get("status", "")),
                    str(e.get("attempts", 0)),
                    str(len(missing) if isinstance(missing, list) else missing),
                    str(e.get("updated_at", "")),
                    str(e.get("notes", "")),
                )
                for c, text in enumerate(cells):
                    item = QtWidgets.QTableWidgetItem(text)
                    if c == 0:
                        item.setData(QtCore.Qt.ItemDataRole.UserRole,
                                     str(e.get("job_posting_id", "")))
                        item.setToolTip(str(arts.get("folder", "")))
                    table.setItem(r, c, item)
                if selected and str(e.get("job_posting_id", "")) == selected:
                    reselect = r
        finally:
            table.blockSignals(False)
        if reselect is not None:
            table.selectRow(reselect)

    def _update_counts(self) -> None:
        counts = {s: 0 for s in apply_queue.STATUSES}
        for e in self._jobs:
            s = e.get("status")
            if s in counts:
                counts[s] += 1
        parts = [f"{s}: {n}" for s, n in counts.items() if n]
        parts.append(f"total: {len(self._jobs)}")
        self.counts_label.setText(" · ".join(parts))

    def refresh_password_state(self) -> None:
        try:
            exists = bool(self._password_exists())
        except Exception:  # noqa: BLE001 - a keyring hiccup must never break the panel
            exists = False
        self.pw_label.setText("Master password: SET" if exists
                              else "Master password: NOT SET")
        self.copy_pw_btn.setEnabled(exists)

    # ---- selection / details --------------------------------------------------------

    def _selected_job_id(self) -> str:
        rows = self.table.selectionModel().selectedRows() \
            if self.table.selectionModel() else []
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "") if item else ""

    def _selected_entry(self) -> Optional[Dict[str, Any]]:
        jid = self._selected_job_id()
        if not jid:
            return None
        for e in self._jobs:
            if str(e.get("job_posting_id", "")) == jid:
                return e
        return None

    def _update_details(self) -> None:
        e = self._selected_entry()
        if e is None:
            self.details.setPlainText("")
            return
        lines = [f"{e.get('company', '')} — {e.get('title', '')}  "
                 f"[{e.get('status', '')}]",
                 f"Apply URL: {e.get('apply_url', '')}"]
        if e.get("notes"):
            lines.append(f"Notes: {e['notes']}")
        if e.get("tab_note"):
            lines.append(f"Parked tab: {e['tab_note']}")
        missing = e.get("missing_answers") or []
        if missing:
            lines.append("Missing answers:")
            for m in missing:
                if isinstance(m, dict):
                    q = m.get("question", "")
                    extra = " — ".join(x for x in (m.get("context", ""),
                                                   m.get("suggestion", "")) if x)
                    lines.append(f"  • {q}" + (f"  ({extra})" if extra else ""))
                else:
                    lines.append(f"  • {m}")
        arts = e.get("artifacts") or {}
        shown = [(k, v) for k, v in arts.items() if v]
        if shown:
            lines.append("Artifacts:")
            lines.extend(f"  {k}: {v}" for k, v in shown)
        self.details.setPlainText("\n".join(lines))

    # ---- actions (all mutations ride submit_write) -----------------------------------

    def _set_note(self, msg: str) -> None:
        self.status_label.setText(msg)

    def _write_failed(self, exc: BaseException) -> None:
        self._set_note(f"Queue write failed: {exc}")

    def _requeue(self) -> None:
        jid = self._selected_job_id()
        if not jid:
            self._set_note("Select a row to re-queue.")
            return
        qp = self._queue_file()
        self._submit_write(
            lambda: apply_queue.requeue(jid, refresh_answers=True, path=qp),
            on_done=lambda _r: self.refresh(), on_error=self._write_failed)

    def _remove(self) -> None:
        jid = self._selected_job_id()
        if not jid:
            self._set_note("Select a row to remove.")
            return
        qp = self._queue_file()
        self._submit_write(lambda: apply_queue.remove(jid, path=qp),
                           on_done=lambda _r: self.refresh(),
                           on_error=self._write_failed)

    def _mark_applied(self) -> None:
        e = self._selected_entry()
        if e is None:
            self._set_note("Select a row to mark applied.")
            return
        self._on_mark_applied(e)
        self._set_note(f"Marked applied — {e.get('company', '')} moved to the Tracker.")

    def _dont_apply(self) -> None:
        e = self._selected_entry()
        if e is None:
            self._set_note("Select a row first.")
            return
        self._on_mark_seen(e)
        self._set_note(
            f"Won't apply — {e.get('company', '')} removed from the queue (still under All Jobs).")

    def _copy_password(self) -> None:
        if ats_accounts.copy_password_to_clipboard():
            self._set_note("Master password copied to the clipboard — paste it, then click Clear.")
        else:
            self._set_note("No master password stored — click 'Set…' first.")

    def _clear_password(self) -> None:
        if ats_accounts.clear_clipboard_if_password():
            self._set_note("Clipboard cleared.")
        else:
            self._set_note("Clipboard left untouched (nothing to clear).")

    def _clear_finished(self) -> None:
        qp = self._queue_file()
        self._submit_write(lambda: apply_queue.clear_finished(path=qp),
                           on_done=lambda _r: self.refresh(),
                           on_error=self._write_failed)

    def _open_artifact(self, key: str, friendly: str) -> None:
        e = self._selected_entry()
        path = str(((e or {}).get("artifacts") or {}).get(key) or "")
        if not path or not Path(path).exists():
            self._set_note(f"No {friendly} on disk for the selected job.")
            return
        try:
            osopen.open_path(path)
        except OSError as exc:
            self._set_note(f"Could not open {path}: {exc}")

    def _open_folder(self) -> None:
        self._open_artifact("folder", "job folder")

    def _open_record(self) -> None:
        self._open_artifact("application_record", "application record")

    def _queued_count(self) -> int:
        """The 'queued' status count from the same jobs list _update_counts
        renders into counts_label — never re-parse that label's text."""
        return sum(1 for e in self._jobs if e.get("status") == "queued")

    def _batch_cap(self, queued: int) -> int:
        """N for the confirm dialog: min(queued, configured batch cap), read
        tolerantly — any exception (bad config.json, missing master yaml,
        etc.) falls back to just the queued count."""
        try:
            cap = int(apply_queue.build_context()["batch_cap"])
            return min(queued, cap)
        except Exception:  # noqa: BLE001 - config hiccups must never block the button
            return queued

    def _ask_run_mode(self, n: int) -> Optional[str]:
        """Ask which permission mode to launch, returning 'unattended', 'scoped',
        or None (cancelled). Three explicit buttons on a QMessageBox — tests
        monkeypatch this method to choose a mode without driving a live modal."""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Start auto-apply run")
        box.setText(
            f"Start an auto-apply run in a new terminal? It works through up to "
            f"{n} queued job(s), parks each at its review page — nothing is ever "
            f"submitted.\n\n"
            f"Unattended: no per-action approval prompts (walk away).\n"
            f"Scoped (safer): pre-approves only the tools the drain needs; it "
            f"pauses if it reaches for anything else.")
        unattended_btn = box.addButton(
            "Unattended", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        scoped_btn = box.addButton(
            "Scoped (safer)", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(scoped_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is unattended_btn:
            return "unattended"
        if clicked is scoped_btn:
            return "scoped"
        return None

    def _start_run(self) -> None:
        """Guards, in order: password set -> queue non-empty -> mode choice.
        Only a chosen mode (unattended/scoped, not Cancel) calls the injected
        on_start_run (default: _spawn_kickoff, a brand-new visible PowerShell
        console), passing the scoped flag through to it."""
        try:
            has_password = bool(self._password_exists())
        except Exception:  # noqa: BLE001 - a keyring hiccup must never crash the panel
            has_password = False
        if not has_password:
            QtWidgets.QMessageBox.warning(
                self, "Master password not set",
                "Set the master password first (the 'Set…' button above) — "
                "the auto-apply run needs it to sign in to ATS accounts.")
            return
        queued = self._queued_count()
        if queued == 0:
            QtWidgets.QMessageBox.information(
                self, "Queue is empty",
                "Queue is empty — queue jobs from the Jobs tab first.")
            return
        n = self._batch_cap(queued)
        mode = self._ask_run_mode(n)
        if mode is None:
            return
        scoped = mode == "scoped"
        self._on_start_run(scoped)
        which = "scoped (safer)" if scoped else "unattended"
        self._set_note(f"Auto-apply run started ({which}) in a new terminal window.")
