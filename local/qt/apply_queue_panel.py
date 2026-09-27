"""The "Auto-apply" tab: a live, read-only mirror of the batch auto-apply queue.

The queue file itself (local/apply_queue.py) is the single source of
truth shared with the drain CLI (local/apply_run.py); this panel only
*displays* it and offers the few human controls around it: Re-queue / Remove /
Clear finished, opening a job's artifacts, the master-password state, "Sign in
to sites" (the one-time `apply_run.py login`), "Copy kickoff command" (the
exact PowerShell line that starts the drain) and "Start auto-apply run" (off,
with the reason beside it, while Jev cannot run: `refresh_jev_state`).

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
import html
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6 import QtCore, QtWidgets

import apply_queue
import ats_accounts
import errmsg
import jev_switch
import osopen
from qt import theme
from qt.chrome import ChipBar, Pill
from qt.delegates import STATUS_LABELS, STATUS_TAGS, TAG_ROLE, JobRowDelegate
from qt.widgets import ElidedLabel

# The repo root (this file lives in <root>/local/qt/): the console commands
# cd here first so the relative `local/apply_run.py` resolves.
REPO_ROOT = Path(__file__).resolve().parents[2]



def _console_command(root: Path, verb: str) -> str:
    """The PowerShell line that runs `apply_run.py <verb>` from `root`.

    5.1-safe: `;` chains (no `&&`). The path goes through
    `Set-Location -LiteralPath '<root>'`: a single-quoted string is literal in
    PowerShell (no `$` expansion, no backtick escapes) and `-LiteralPath` keeps
    a `[` from reading as a wildcard, so a checkout under any folder name
    resolves. The one escape a single-quoted string knows is a doubled quote.
    """
    literal = str(root).replace("'", "''")
    return f"Set-Location -LiteralPath '{literal}'; python local/apply_run.py {verb}"


# The drain is this project's own code (local/apply_run.py): it claims each
# queued job, drives a persistent Chromium profile through the application
# with the Jev judge, and submits only when the confidence gate passes
# (`auto_apply_submit` in Settings, `--no-submit` on the command line parks
# every job at its review page instead). It refuses to start while Jev cannot
# run (switched off, no key, no SDK), and the Start button is off then too.
KICKOFF_COMMAND = _console_command(REPO_ROOT, "drain")

# The one-time sign-in: opens the same persistent profile, headed, at
# LinkedIn's login and the configured inbox, and waits for the window to close.
LOGIN_COMMAND = _console_command(REPO_ROOT, "login")


def _console_argv(command: str) -> list[str]:
    """The argv that opens a NEW PowerShell console running `command`.

    Pure and testable: no subprocess call here. The command embeds the literal
    REPO_ROOT (`Set-Location -LiteralPath '<root>'`), and base64 via -EncodedCommand sidesteps
    PowerShell 5.1's quoting rules entirely (no re-tokenizing, no escaping) and
    round-trips cleanly for the tests to decode.
    """
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoExit", "-EncodedCommand", encoded]


def _kickoff_argv() -> list[str]:
    """The console argv for KICKOFF_COMMAND (`apply_run.py drain`)."""
    return _console_argv(KICKOFF_COMMAND)


def _login_argv() -> list[str]:
    """The console argv for LOGIN_COMMAND (`apply_run.py login`)."""
    return _console_argv(LOGIN_COMMAND)


def _spawn_console(argv: list[str]) -> None:
    """Launch `argv` in a brand-new, visible console.

    The child inherits this process's environment on purpose: `apply_run.py`
    is this project's own code and loads `.env` itself, so a first pasted key
    reaches it. The Settings tab's TYPESAFE_API_KEY row is marked `restart`
    because a rotated key sits behind the dashboard's startup snapshot until a
    restart. The flag is guarded via getattr so importing
    this module on a non-Windows box (CI, a dev's Mac) never raises at import
    time.
    """
    subprocess.Popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def _spawn_kickoff() -> None:
    """Default on_start_run: the drain in a new console."""
    _spawn_console(_kickoff_argv())


def _spawn_login() -> None:
    """Default on_login: the one-time sign-in in a new console."""
    _spawn_console(_login_argv())


class _ShrinkableCaption(ElidedLabel):
    """An `ElidedLabel` that still asks for its full width.

    `ElidedLabel` declares an Ignored horizontal policy, which is right for the
    one caption in a row that shares the row with a stretch, and wrong inside a
    fixed cluster: with no stretch to claim, Ignored means Qt hands it zero width
    even on a 1920px window, so the caption disappears entirely. Preferred asks
    for the whole string; a zero minimum still lets a crowded row take it back a
    character at a time, which is the point.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Preferred)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        """Width of the FULL caption, not of the elision currently painted.

        `ElidedLabel` elides by writing the shortened string into QLabel itself,
        so QLabel's own sizeHint measures the truncated text — asking for less
        room, which elides it further. Left alone it ratchets to nothing and
        never recovers when the window is widened again.
        """
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        return QtCore.QSize(
            fm.horizontalAdvance(self.text()) + margins.left() + margins.right(),
            super().sizeHint().height())

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return QtCore.QSize(0, super().minimumSizeHint().height())


COLUMNS = ("Company", "Title", "Status", "Attempts", "Missing", "Updated", "Note")
# Column ids the row delegate keys its renderers on (status pill + dot,
# right-aligned mono counts, mono muted timestamp), 1:1 with COLUMNS.
COLUMN_IDS = ("company", "title", "status", "attempts", "missing", "updated", "note")

_DEBOUNCE_MS = 500     # coalesce a burst of fs events into one refresh
_POLL_MS = 5000        # mtime-poll fallback when no fs events arrive


def _default_password_exists() -> bool:
    """Panel seam for the master-password state (module-level so tests patch it
    without ever querying the real Windows Credential Manager)."""
    return ats_accounts.password_exists()


def _default_jev_blocked() -> str:
    """Panel seam for the Jev gate (JS-5): why a run cannot start, in the words
    `jev_switch.apply_blocked` gives the Start button and `apply_run.py drain`
    alike, or "" when it can. The drain Start launches has no --jev flag, so
    the gate reads the mode that drain reads (`jev_switch.apply_mode`). A key
    saved in Settings counts (`jev_switch.key_saved`): the drain's console
    loads `.env` itself, so the key reaches it before the dashboard restarts."""
    return jev_switch.apply_blocked(saved_key=jev_switch.key_saved())


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


class _DetailsPanel(QtWidgets.QFrame):
    """The structured details card under the queue table.

    Header (title + status pill), muted meta line, a WHY PAUSED/NOTES lede, a
    warning callout listing the missing answers with an "Answer now" jump to the
    Apply Answers tab, and a mono artifacts block. `toPlainText()` composes the
    same text the card paints, and the tests read it (missing questions must
    appear there), so the two have to stay in step.
    """

    _EMPTY = ("Select a queued application to see its status, answers, "
              "and history.")

    def __init__(self, on_answer_now: Callable[[], None] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._plain = ""
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)

        self.empty_label = QtWidgets.QLabel(self._EMPTY)
        self.empty_label.setStyleSheet(f"color: {theme.FAINT};")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty_label)

        self._content = QtWidgets.QWidget()
        v.addWidget(self._content)
        cv = QtWidgets.QVBoxLayout(self._content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        # PlainText for every label fed a raw scraped string (company, title,
        # location, artifact paths). Qt's AutoText default would run
        # Qt::mightBeRichText over them and render a posting's stray markup;
        # the lede and callout below stay RichText because we compose their
        # markup ourselves and html.escape() the untrusted part into it.
        self.title_label = QtWidgets.QLabel("")
        self.title_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        theme.set_type_role(self.title_label, "section")
        head.addWidget(self.title_label)
        self.status_pill = Pill("", "neutral")
        head.addWidget(self.status_pill)
        head.addStretch(1)
        self.open_record_btn = QtWidgets.QPushButton("Open application record ↗")
        self.open_record_btn.setProperty("tier", "link")
        head.addWidget(self.open_record_btn)
        self.open_folder_btn = QtWidgets.QPushButton("Open job folder")
        head.addWidget(self.open_folder_btn)
        cv.addLayout(head)

        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.meta_label.setProperty("muted", True)
        cv.addWidget(self.meta_label)

        self.lede_label = QtWidgets.QLabel("")
        self.lede_label.setWordWrap(True)
        self.lede_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        cv.addWidget(self.lede_label)

        self.callout = QtWidgets.QFrame()
        self.callout.setProperty("callout", "warning")
        wh = QtWidgets.QHBoxLayout(self.callout)
        wh.setContentsMargins(12, 8, 12, 8)
        wh.setSpacing(10)
        self.callout_label = QtWidgets.QLabel("")
        self.callout_label.setWordWrap(True)
        wh.addWidget(self.callout_label, 1)
        self.answer_now_btn = QtWidgets.QPushButton("Answer now")
        self.answer_now_btn.setToolTip(
            "Open the Apply Answers tab, save the missing answer(s) to the "
            "answer bank, then Re-queue this job")
        self.answer_now_btn.clicked.connect(on_answer_now or (lambda: None))
        wh.addWidget(self.answer_now_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        cv.addWidget(self.callout)

        self.artifacts_label = QtWidgets.QLabel("")
        self.artifacts_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.artifacts_label.setWordWrap(True)
        self.artifacts_label.setProperty("muted", True)
        theme.set_type_role(self.artifacts_label, "mono")
        cv.addWidget(self.artifacts_label)

        self.set_entry(None)

    # -- content --------------------------------------------------------------

    def set_entry(self, e: Optional[Dict[str, Any]]) -> None:
        if e is None:
            self._plain = ""
            self._content.setVisible(False)
            self.empty_label.setVisible(True)
            return
        self.empty_label.setVisible(False)
        self._content.setVisible(True)

        status = str(e.get("status", ""))
        self.title_label.setText(f"{e.get('company', '')}: {e.get('title', '')}")
        self.status_pill.setText(STATUS_LABELS.get(status, status.capitalize()))
        self.status_pill.set_family(STATUS_TAGS.get(status, "neutral"))

        meta = [m for m in (str(e.get("apply_url", "")),
                            f"{e.get('attempts', 0)} attempt(s)") if m]
        if e.get("updated_at"):
            meta.append(f"updated {e['updated_at']}")
        self.meta_label.setText(" · ".join(meta))

        lede_tag = {"needs_human": "WHY PAUSED", "failed": "WHY FAILED"}.get(
            status, "NOTES")
        lede_parts = [p for p in (str(e.get("notes") or ""),
                                  str(e.get("tab_note") or "")) if p]
        lede = " | ".join(lede_parts)
        if lede:
            self.lede_label.setText(
                f'<span style="color:{theme.MUTED};font-weight:600;'
                f'letter-spacing:0.4px">{lede_tag}</span>&nbsp;&nbsp;'
                f'<span style="color:{theme.TEXT_SECONDARY}">'
                f'{html.escape(lede)}</span>')
        self.lede_label.setVisible(bool(lede))

        missing = e.get("missing_answers") or []
        questions: List[str] = []
        for m in missing:
            if isinstance(m, dict):
                q = m.get("question", "")
                extra = "; ".join(x for x in (m.get("context", ""),
                                               m.get("suggestion", "")) if x)
                questions.append(f"{q}" + (f"  ({extra})" if extra else ""))
            else:
                questions.append(str(m))
        if questions:
            listed = "<br>".join(
                f'&nbsp;•&nbsp;{html.escape(q)}' for q in questions)
            self.callout_label.setText(
                f'<b>Missing answer{"s" if len(questions) > 1 else ""}</b><br>'
                f'{listed}<br>'
                f'<span style="color:{theme.MUTED}">Your answer is saved to the '
                f'answer bank, and fills this question again wherever a later '
                f'application words it the same way.</span>')
        self.callout.setVisible(bool(questions))

        arts = e.get("artifacts") or {}
        shown = [(k, val) for k, val in arts.items() if val]
        self.artifacts_label.setText(
            "\n".join(f"{k}: {val}" for k, val in shown))
        self.artifacts_label.setVisible(bool(shown))

        # Plain-text mirror — SAME composition the old QPlainTextEdit held.
        lines = [f"{e.get('company', '')}: {e.get('title', '')}  [{status}]",
                 f"Apply URL: {e.get('apply_url', '')}"]
        if e.get("notes"):
            lines.append(f"Notes: {e['notes']}")
        if e.get("tab_note"):
            lines.append(f"Parked tab: {e['tab_note']}")
        if questions:
            lines.append("Missing answers:")
            lines.extend(f"  • {q}" for q in questions)
        if shown:
            lines.append("Artifacts:")
            lines.extend(f"  {k}: {val}" for k, val in shown)
        self._plain = "\n".join(lines)

    def toPlainText(self) -> str:  # noqa: N802 (mirrors the old QPlainTextEdit API)
        return self._plain


class ApplyQueuePanel(QtWidgets.QWidget):
    """Header (counts / password state / Start auto-apply run) + queue table +
    details pane + the Re-queue / Remove / Clear finished / Open buttons."""

    def __init__(self, queue_path: Path | str | None = None, *,
                 submit_write: Callable | None = None,
                 on_set_password: Callable[[], None] | None = None,
                 password_exists: Callable[[], bool] | None = None,
                 on_start_run: Callable[[], None] | None = None,
                 on_login: Callable[[], None] | None = None,
                 on_mark_applied: Callable[[Dict[str, Any]], None] | None = None,
                 on_mark_seen: Callable[[Dict[str, Any]], None] | None = None,
                 on_answer_now: Callable[[], None] | None = None,
                 jev_blocked: Callable[[], str] | None = None,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue_override = Path(queue_path) if queue_path else None
        self._submit_write = submit_write or _run_inline
        self._on_set_password = on_set_password or (lambda: None)
        # Late-bound default so a monkeypatched module seam takes effect.
        self._password_exists = password_exists or (lambda: _default_password_exists())
        self._on_start_run = on_start_run or _spawn_kickoff
        self._on_login = on_login or _spawn_login
        self._on_mark_applied = on_mark_applied or (lambda _e: None)
        self._on_mark_seen = on_mark_seen or (lambda _e: None)
        # "Answer now" on the missing-answers callout — the main window wires
        # this to switch to the Apply Answers tab.
        self._on_answer_now = on_answer_now or (lambda: None)
        # Late-bound like password_exists: why a run cannot start ("" when it can).
        self._jev_blocked = jev_blocked or (lambda: _default_jev_blocked())
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

        # Header, two rows: what the queue is doing (chips + counts caption) over
        # what you can do about it (master password, Start). One row held these
        # four side by side and needed 1428px at 150%, so on anything narrower
        # than a maximised window Qt took the deficit out of the status chips and
        # they read "Ready to su" / "Needs revie". Splitting also groups the row
        # by what it is rather than by what fit.
        header = QtWidgets.QHBoxLayout()
        self.status_chips = ChipBar(
            [(s, STATUS_LABELS.get(s, s.capitalize()),
              theme.SEMANTICS[STATUS_TAGS[s]]["base"])
             for s in ("queued", "in_progress", "ready_to_submit", "needs_human")],
            checkable=False)
        header.addWidget(self.status_chips)
        # The per-status counts caption is the longest thing in this row and the
        # least informative (every count it names is already on a chip). It is
        # the one element with no minimum width, so at 1280 the status chips keep
        # their labels ("Ready to submit", not "Ready to su") and the caption
        # elides instead.
        self.counts_label = ElidedLabel("")
        self.counts_label.setProperty("muted", True)
        header.addWidget(self.counts_label, 1)

        # Master-password cluster: caption/state label + SET pill + compact
        # buttons in one bordered frame. pw_label keeps its exact text contract
        # ("Master password: SET/NOT SET") as the cluster's accessible label.
        cluster = QtWidgets.QFrame()
        cluster.setProperty("card", True)
        ch = QtWidgets.QHBoxLayout(cluster)
        ch.setContentsMargins(12, 4, 8, 4)
        ch.setSpacing(8)
        # Elides, for the same reason the counts caption does: at 125% and 150%
        # the header row wants more width than a 1100-1280px window has, and once
        # the deficit outran the counts caption Qt took the rest out of the status
        # chips, which read "Ready to su" / "Needs revie". This caption is the
        # next-least informative thing in the row — the pill beside it says SET
        # or NOT SET in words either way — so it gives ground next. `.text()`
        # still returns the full string, which is the cluster's accessible label.
        self.pw_label = _ShrinkableCaption("")
        self.pw_label.setProperty("muted", True)
        ch.addWidget(self.pw_label)
        self.pw_pill = Pill("NOT SET", "neutral")
        ch.addWidget(self.pw_pill)
        self.pw_btn = QtWidgets.QPushButton("Set…")
        self.pw_btn.setToolTip(
            "Store the ONE master password every auto-created ATS account uses "
            "(Windows Credential Manager; never written to any file)")
        self.pw_btn.clicked.connect(lambda: self._on_set_password())
        ch.addWidget(self.pw_btn)
        self.copy_pw_btn = QtWidgets.QPushButton("Copy")
        self.copy_pw_btn.setToolTip(
            "Copy the master password to the clipboard for a manual login; it is "
            "never shown on screen. Clears from the clipboard when you click Clear.")
        self.copy_pw_btn.clicked.connect(self._copy_password)
        ch.addWidget(self.copy_pw_btn)
        self.clear_pw_btn = QtWidgets.QPushButton("Clear")
        self.clear_pw_btn.setProperty("tier", "destructive")
        self.clear_pw_btn.setToolTip("Clear the master password from the clipboard.")
        self.clear_pw_btn.clicked.connect(self._clear_password)
        ch.addWidget(self.clear_pw_btn)
        v.addLayout(header)

        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(cluster)
        actions.addStretch(1)

        self.copy_cmd_btn = QtWidgets.QPushButton("Copy kickoff command")
        self.copy_cmd_btn.setProperty("tier", "tertiary")
        self.copy_cmd_btn.setToolTip(
            "Copy the exact command the Start button runs (python "
            "local/apply_run.py drain) to paste into a terminal of your own; add "
            "--no-submit there to park every application at its review page.")
        self.copy_cmd_btn.clicked.connect(self._copy_kickoff)
        actions.addWidget(self.copy_cmd_btn)
        self.login_btn = QtWidgets.QPushButton("Sign in to sites")
        self.login_btn.setToolTip(
            "One-time setup: opens the auto-apply browser profile at LinkedIn "
            "and your inbox so you can sign in to both. Close the window when "
            "you are done; the run reuses those sessions.")
        self.login_btn.clicked.connect(self._sign_in)
        actions.addWidget(self.login_btn)
        self.start_run_btn = QtWidgets.QPushButton("Start auto-apply run")
        self.start_run_btn.setProperty("accent", True)
        # Kept for refresh_jev_state, which shows the Jev reason in its place.
        self._start_tip = (
            "Launch the auto-apply drain in a NEW terminal window: click once, "
            "walk away. It works through up to batch_cap queued jobs in its own "
            "browser profile, filling each form from your apply sheet with the "
            "Jev judge, and submits when every required field is filled and "
            "verified with no CAPTCHA, payment or blocked question on the page; "
            "anything less parks at the review page for you. Turn 'Submit when "
            "verified' off in Settings to park every job.")
        self.start_run_btn.setToolTip(self._start_tip)
        self.start_run_btn.clicked.connect(self._start_run)
        actions.addWidget(self.start_run_btn)
        v.addLayout(actions)

        # Why Start is off while Jev cannot run (JS-5), in the drain's words;
        # hidden while a run can start. Queueing goes on either way.
        self.jev_notice = QtWidgets.QFrame()
        self.jev_notice.setProperty("callout", "warning")
        jn = QtWidgets.QHBoxLayout(self.jev_notice)
        jn.setContentsMargins(12, 8, 12, 8)
        self.jev_label = QtWidgets.QLabel("")
        self.jev_label.setWordWrap(True)
        jn.addWidget(self.jev_label, 1)
        self.jev_notice.setVisible(False)
        v.addWidget(self.jev_notice)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        # The delegate paints the cells (status tint from TAG_ROLE, pills,
        # separators) — zebra/grid off so nothing repaints over its layers.
        self.table.setItemDelegate(JobRowDelegate(
            list(COLUMN_IDS), kind="apply", parent=self.table))
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        theme.register_table(self.table)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        # Widths are @100% and scale with the live interface size, the same way
        # JobsTab._set_column_widths does.
        scale = theme._current_scale
        for i, w in enumerate((150, 220, 150, 90, 80, 150, 200)):
            self.table.setColumnWidth(i, round(w * scale))
        # ...except the two count columns, which size themselves. A header
        # section is the one piece of table text Qt clips instead of eliding, so
        # a column narrower than its own title silently loses the first and last
        # letter: "Attempts" and "Missing" shipped at 70px and 60px with 18px of
        # QSS padding inside them and read as "ttempt" and "lissin". A pinned
        # width only moves that failure to another interface scale, and neither
        # column holds anything a user would want to drag wider than its own
        # heading, so let the header measure them instead.
        for i in (COLUMN_IDS.index("attempts"), COLUMN_IDS.index("missing")):
            hh.setSectionResizeMode(
                i, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_details)
        v.addWidget(self.table, 1)
        self._auto_width_columns = (COLUMN_IDS.index("attempts"),
                                    COLUMN_IDS.index("missing"))

        # Structured details panel. It keeps a `toPlainText()` mirror of the composed
        # text because the tests assert against that flattened form.
        self.details = _DetailsPanel(on_answer_now=lambda: self._on_answer_now())
        self.open_folder_btn = self.details.open_folder_btn
        self.open_record_btn = self.details.open_record_btn
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.open_record_btn.clicked.connect(self._open_record)
        v.addWidget(self.details)

        btns = QtWidgets.QHBoxLayout()

        def button(text, slot, tip="", tier=""):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            if tip:
                b.setToolTip(tip)
            if tier:
                b.setProperty("tier", tier)
            btns.addWidget(b)
            return b

        self.requeue_btn = button(
            "Re-queue", self._requeue,
            "Send the selected job back to 'queued' (clears its missing answers "
            "and refreshes its apply.md standard answers and address)")
        self.mark_applied_btn = button(
            "Mark applied", self._mark_applied,
            "Move to the Tracker as applied and remove from the queue")
        self.dont_apply_btn = button(
            "Don't apply", self._dont_apply,
            "Mark seen (keeps it in All Jobs) and remove from the queue",
            tier="tertiary")
        self.clear_btn = button(
            "Clear finished", self._clear_finished,
            "Drop every ready_to_submit / submitted / needs_human / failed entry",
            tier="tertiary")
        self.remove_btn = button("Remove from queue", self._remove,
                                 "Delete the selected entry from the queue",
                                 tier="destructive")
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
        self.refresh_jev_state()
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
                status = str(e.get("status", ""))
                for c, text in enumerate(cells):
                    item = QtWidgets.QTableWidgetItem(text)
                    # Paint-time row tag: the delegate tints the whole row and
                    # pills the Status cell from it. DisplayRole stays the RAW
                    # status text (test-coupled; the pill label is paint-only).
                    item.setData(TAG_ROLE, status)
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

    def rescale_columns(self, factor: float) -> None:
        """Re-scale the live column widths by `factor` after an interface-scale
        change (the widths above are only applied at construction). The two
        ResizeToContents columns measure themselves and are left alone."""
        if factor <= 0 or abs(factor - 1.0) < 1e-9:
            return
        hh = self.table.horizontalHeader()
        for i in range(self.table.columnCount()):
            if i in self._auto_width_columns:
                continue
            self.table.setColumnWidth(i, max(1, round(hh.sectionSize(i) * factor)))

    def _update_counts(self) -> None:
        counts = {s: 0 for s in apply_queue.STATUSES}
        for e in self._jobs:
            s = e.get("status")
            if s in counts:
                counts[s] += 1
        # The same words the chips beside this caption use, not the raw status
        # ids: one row read "In progress 1 · Ready to submit 1 · Needs review 1"
        # on the chips and "in_progress: 1 · ready_to_submit: 1 · needs_human: 1"
        # in the caption immediately to their right — two vocabularies for one
        # set of states, with the machine's spelling the more prominent of the
        # two. Lower-cased because this is a caption, not a set of labels.
        parts = [f"{STATUS_LABELS.get(s, s.replace('_', ' ')).lower()}: {n}"
                 for s, n in counts.items() if n]
        parts.append(f"total: {len(self._jobs)}")
        self.counts_label.setText(" · ".join(parts))
        self.status_chips.set_counts(counts)

    def refresh_password_state(self) -> None:
        try:
            exists = bool(self._password_exists())
        except Exception:  # noqa: BLE001 - a keyring hiccup must never break the panel
            exists = False
        self.pw_label.setText("Master password: SET" if exists
                              else "Master password: NOT SET")
        self.pw_pill.setText("SET" if exists else "NOT SET")
        self.pw_pill.set_family("success" if exists else "neutral")
        self.copy_pw_btn.setEnabled(exists)

    def refresh_jev_state(self) -> str:
        """Start is off, with the reason as its tooltip and in the notice under
        the buttons, while an auto-apply run cannot start on Jev (JS-5). Read
        on every refresh, after a Settings save (the main window calls this)
        and at each Start click. Returns the reason, "" when a run can start."""
        try:
            reason = str(self._jev_blocked() or "")
        except Exception:  # noqa: BLE001 - the drain checks again; never break the panel
            reason = ""
        self.start_run_btn.setEnabled(not reason)
        self.start_run_btn.setToolTip(reason or self._start_tip)
        self.jev_label.setText(reason)
        self.jev_notice.setVisible(bool(reason))
        return reason

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
        self.details.set_entry(self._selected_entry())

    # ---- actions (all mutations ride submit_write) -----------------------------------

    def _set_note(self, msg: str) -> None:
        self.status_label.setText(msg)

    def _write_failed(self, exc: BaseException) -> None:
        self._set_note(f"Queue write failed: {errmsg.for_user(exc)}")

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
        self._set_note(f"Marked applied: {e.get('company', '')} moved to the Tracker.")

    def _dont_apply(self) -> None:
        e = self._selected_entry()
        if e is None:
            self._set_note("Select a row first.")
            return
        self._on_mark_seen(e)
        self._set_note(
            f"Won't apply: {e.get('company', '')} removed from the queue (still under All Jobs).")

    def _copy_password(self) -> None:
        if ats_accounts.copy_password_to_clipboard():
            self._set_note("Master password copied to the clipboard. Paste it, then click Clear.")
        else:
            self._set_note("No master password stored; click 'Set…' first.")

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
            self._set_note(f"Could not open {Path(path).name}: {errmsg.for_user(exc)}")

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

    def _confirm_text(self, n: int) -> str:
        """The confirm dialog's body: how many jobs, and the submit gate."""
        return (f"Start an auto-apply run in a new terminal? It works through up to "
                f"{n} queued job(s) in its own browser window.\n\n"
                f"Each application is filled from that job's apply sheet and "
                f"checked by the Jev judge. The run submits when every required "
                f"field is filled and verified and the page shows no CAPTCHA, "
                f"payment or blocked question; otherwise it parks the job at its "
                f"review page for you. Turn auto_apply_submit off in Settings to "
                f"park every job.")

    def _confirm_run(self, n: int) -> bool:
        """Ask before launching; tests monkeypatch this method to answer
        without driving a live modal."""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Start auto-apply run")
        box.setText(self._confirm_text(n))
        start_btn = box.addButton("Start", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(start_btn)
        box.exec()
        return box.clickedButton() is start_btn

    def _copy_kickoff(self) -> None:
        QtWidgets.QApplication.clipboard().setText(KICKOFF_COMMAND)
        self._set_note("Kickoff command copied to the clipboard.")

    def _sign_in(self) -> None:
        self._on_login()
        self._set_note("Sign in to LinkedIn and your inbox in the new browser window, "
                       "then close it.")

    def _start_run(self) -> None:
        """Guards, in order: Jev can run -> password set -> queue non-empty ->
        confirm. Only a confirmed dialog calls the injected on_start_run
        (default: _spawn_kickoff, a brand-new visible PowerShell console)."""
        reason = self.refresh_jev_state()   # the switch may have moved since
        if reason:
            self._set_note(reason)
            return
        try:
            has_password = bool(self._password_exists())
        except Exception:  # noqa: BLE001 - a keyring hiccup must never crash the panel
            has_password = False
        if not has_password:
            QtWidgets.QMessageBox.warning(
                self, "Master password not set",
                "Set the master password first (the 'Set…' button above); "
                "the auto-apply run needs it to sign in to ATS accounts.")
            return
        queued = self._queued_count()
        if queued == 0:
            QtWidgets.QMessageBox.information(
                self, "Queue is empty",
                "Queue is empty; queue jobs from the Jobs tab first.")
            return
        n = self._batch_cap(queued)
        if not self._confirm_run(n):
            return
        self._on_start_run()
        self._set_note("Auto-apply run started in a new terminal window.")
