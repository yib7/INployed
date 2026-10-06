"""The "Auto-apply" tab: a live, read-only mirror of the batch auto-apply queue.

The queue file itself (local/apply_queue.py) is the single source of
truth shared with the drain CLI (local/apply_run.py); this panel only
*displays* it and offers the few human controls around it: Re-queue / Remove /
Clear finished, opening a job's artifacts, the master-password state, "Sign in
to sites" (the one-time `apply_run.py login`), "Copy kickoff command" (the
exact PowerShell line that starts the drain) and "Start auto-apply run" (off,
with the reason beside it, while the drain it launches would refuse to start:
`refresh_jev_state`).

The difficulty check: the Difficulty column shows each
job's 1-10 score as a pill coloured by its band, with the reasons and the exact
questions in its tooltip and the age of a result older than a week. "Check
difficulty" runs `apply_assess.py` in its own console (hidden while the check
is switched off, off with the reason while Jev cannot run or a browser holds
the auto-apply profile).

Several rows: the table takes a Ctrl/Shift-click selection. Check
difficulty checks every selected job, several at once (`auto_apply_check_parallel`,
read by the check's console), and Remove drops them all in one write after a
confirm. Meanwhile the buttons that act on one job (Re-queue, Mark applied,
Don't apply, and the details card's) are off with the tooltip "Select one job".

Park and resume: a run that pauses on a question it cannot
answer writes a request into `apply_pause.pause_dir()`; the 5 s poll reads the
folder and shows the oldest request in the "Waiting for you" card at the top
(`qt.apply_pause_card.PauseCard`), and a new request flashes the taskbar
(`QApplication.alert`). "Answer now" on a parked job's missing answers opens
Add answer prefilled with the question (its help and options kept by the run),
and once the answer is saved offers to Re-queue the job.

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
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6 import QtCore, QtWidgets

import apply_assess
import apply_pause
import apply_queue
import assess_pool
import ats_accounts
import errmsg
import jev_switch
import osopen
import profile_lock
from qt import theme
from qt.apply_pause_card import PauseCard
from qt.chrome import ChipBar, Pill
from qt.delegates import BAND_ROLE, STATUS_LABELS, STATUS_TAGS, TAG_ROLE, JobRowDelegate
from qt.widgets import ElidedLabel
from qt.plaintext import Label, literal

# The repo root (this file lives in <root>/local/qt/): the console commands
# cd here first so the relative `local/apply_run.py` resolves.
REPO_ROOT = Path(__file__).resolve().parents[2]



# PowerShell ends a single-quoted string at any of these, the curly ones too
_PS_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")

# A job id the check passes on the command line: digits for a LinkedIn job,
# `manual-<hex>` for one added by hand
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9_-]+")


def _ps_literal(text: str) -> str:
    """`text` as a PowerShell single-quoted string. The one escape such a
    string knows is a doubled quote, and every quote character is doubled."""
    for quote in _PS_QUOTES:
        text = text.replace(quote, quote * 2)
    return f"'{text}'"


def _console_command(root: Path, verb: str) -> str:
    """The PowerShell line that runs `apply_run.py <verb>` from `root`.

    5.1-safe: `;` chains (no `&&`). The path goes through
    `Set-Location -LiteralPath '<root>'`: a single-quoted string is literal in
    PowerShell (no `$` expansion, no backtick escapes) and `-LiteralPath` keeps
    a `[` from reading as a wildcard, so a checkout under any folder name
    resolves.
    """
    return f"Set-Location -LiteralPath {_ps_literal(str(root))}; python local/apply_run.py {verb}"


# The drain is this project's own code (local/apply_run.py): it claims each
# queued job, drives a persistent Chromium profile through the application
# with the Jev judge, and submits only when the confidence gate passes
# (`auto_apply_submit` in Settings, `--no-submit` on the command line parks
# every job at its review page instead). It refuses to start on a test judge
# (fake, replay) or a judge mode it cannot build, while Jev cannot run
# (switched off, no key, no SDK), and while another browser holds the
# auto-apply profile (`profile_lock`); the Start button is off then too.
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


def _assess_command(root: Path, job_ids: list[str]) -> str:
    """The PowerShell line that runs the difficulty check from `root`: the
    named jobs, or every queued job (`--all`) when none is named. Each id is
    a single-quoted literal, the root as in `_console_command`. Raises
    ValueError for an id outside letters, digits, `_` and `-`."""
    for jid in job_ids:
        if not (isinstance(jid, str) and _SAFE_JOB_ID.fullmatch(jid)):
            raise ValueError(f"a job id the check cannot pass on: {jid!r}")
    args = [_ps_literal(jid) for jid in job_ids] or ["--all"]
    return (f"Set-Location -LiteralPath {_ps_literal(str(root))}; "
            "python local/apply_assess.py " + " ".join(args))


def _spawn_check(job_ids: list[str]) -> None:
    """Default on_check_difficulty: `apply_assess.py` in a new console."""
    _spawn_console(_console_argv(_assess_command(REPO_ROOT, job_ids)))


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


COLUMNS = ("Company", "Title", "Status", "Attempts", "Missing", "Difficulty", "Updated",
           "Note")
# Column ids the row delegate keys its renderers on (status pill + dot,
# right-aligned mono counts, the difficulty pill, mono muted timestamp), 1:1
# with COLUMNS.
COLUMN_IDS = ("company", "title", "status", "attempts", "missing", "difficulty", "updated",
              "note")

_DEBOUNCE_MS = 500     # coalesce a burst of fs events into one refresh
_POLL_MS = 5000        # mtime-poll fallback when no fs events arrive


def _default_password_exists() -> bool:
    """Panel seam for the master-password state (module-level so tests patch it
    without ever querying the real Windows Credential Manager)."""
    return ats_accounts.password_exists()


def _default_jev_blocked() -> str:
    """Panel seam for the Start gate: why the drain Start launches would refuse
    to start, in the words `apply_run.py drain` prints, or "" when it would run
    (`jev_switch.start_blocked`: the drain's refusal of a test judge or an
    unknown one, then the Jev gate). The drain has no --jev flag, so the gate reads the mode
    that drain reads (`jev_switch.apply_mode`). A key saved in Settings counts
    (`jev_switch.key_saved`): the drain's console loads `.env` itself, so the
    key reaches it before the dashboard restarts."""
    return jev_switch.start_blocked(saved_key=jev_switch.key_saved())


def _default_difficulty_blocked() -> str:
    """Panel seam for Check difficulty: why `apply_assess.py` would refuse,
    in its own words, or "" when it would run (`apply_assess.refusal`: a test
    judge first, then the Jev gate for the check). A key saved in Settings
    counts, as for Start: the check's console loads `.env` itself."""
    return apply_assess.refusal(saved_key=jev_switch.key_saved())


def _default_difficulty_hidden() -> bool:
    """Panel seam: is the check switched off in Settings (the Jev master
    switch or its own)? The buttons hide then."""
    return jev_switch.switched_off("difficulty")


def _default_check_parallel() -> int:
    """Panel seam: how many difficulty checks run at once (the
    `auto_apply_check_parallel` setting, clamped 1 to 10 the way
    `apply_assess.py` reads it); the default when the settings do not read."""
    try:
        import settings
        value = settings.load().get("auto_apply_check_parallel")
    except Exception:  # noqa: BLE001 - a config hiccup only changes a note
        value = None
    return assess_pool.parallel_setting(value)


def _default_profile_busy() -> bool:
    """Panel seam, read by Start and by Check difficulty: does a browser hold
    the auto-apply profile (Chrome's lock or the sentinel, `profile_lock`)?"""
    return apply_assess.profile_busy()


# The tooltip on a single-job button while several rows are selected.
SELECT_ONE = "Select one job"

UPLOAD_HINT = "an upload: put the file in the job folder"
FAILED_CELL = "check failed"


def _difficulty_text(d: Dict[str, Any]) -> str:
    """The Difficulty cell: "N/10", with the age of a result older than a
    week; "check failed" for a job whose only check read nothing; "" for a
    job the check has not tried."""
    if not isinstance(d, dict):
        return ""
    if not d.get("score"):
        return FAILED_CELL if d.get("last_failed_at") else ""
    age = apply_assess.age_text(str(d.get("checked_at") or ""))
    return f"{d['score']}/10" + (f" ({age})" if age else "")


def _difficulty_tip(d: Dict[str, Any]) -> str:
    """The Difficulty cell's tooltip: the score and band, when it was read,
    the reasons and the exact questions the answers cannot fill (an upload
    says so), with a check that read nothing since (`apply_assess.failed_text`)
    above them. The page's words are escaped into the markup."""
    if not isinstance(d, dict):
        return ""
    esc = html.escape
    failed = apply_assess.failed_text(d)
    if not d.get("score"):
        return f"<qt>{esc(failed)}</qt>" if failed else ""
    age = apply_assess.age_text(str(d.get("checked_at") or ""))
    lines = [f"<b>{esc(str(d['score']))}/10: {esc(str(d.get('band') or ''))}</b>"]
    if failed:
        lines.append(esc(failed))
    lines.append(esc(f"Checked {d.get('checked_at') or '?'}" + (f", {age}" if age else "")))
    reasons = [str(r) for r in d.get("reasons") or []]
    if reasons:
        lines.append("Why:")
        lines += [f"&nbsp;&bull;&nbsp;{esc(r)}" for r in reasons]
    questions = [q for q in d.get("questions") or [] if isinstance(q, dict)]
    if questions:
        lines.append("Questions your answers cannot fill:")
        for q in questions:
            extra = [UPLOAD_HINT] if q.get("type") == "file" else []
            extra.append(str(q.get("help") or ""))
            options = [str(o) for o in q.get("options") or []]
            if options:
                extra.append("options: " + ", ".join(options))
            shown = "; ".join(x for x in extra if x)
            lines.append(f"&nbsp;&bull;&nbsp;{esc(str(q.get('label') or ''))}"
                         + (f" ({esc(shown)})" if shown else ""))
    return "<qt>" + "<br>".join(lines) + "</qt>"


def _missing_prefill(m: Dict[str, Any]) -> Dict[str, Any]:
    """Add answer's prefill for one of a parked job's missing answers:
    the question with the help, options and type the run kept."""
    return {"question": str(m.get("question") or ""), "help": str(m.get("help") or ""),
            "type": str(m.get("type") or ""),
            "options": [str(o) for o in m.get("options") or []]}


def _default_alert(widget: QtWidgets.QWidget) -> None:
    """Flash the dashboard's taskbar button (a new pause request)."""
    QtWidgets.QApplication.alert(widget.window(), 0)


def _pending_requests() -> List[Dict[str, Any]]:
    """The waiting pause requests, oldest first; [] when the folder does not
    read (the panel never breaks on it)."""
    try:
        return apply_pause.pending_requests()
    except Exception:  # noqa: BLE001 - a folder hiccup must never break the panel
        return []


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

        self.empty_label = Label(self._EMPTY)
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
        self.title_label = Label("")
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

        self.meta_label = Label("")
        self.meta_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.meta_label.setProperty("muted", True)
        cv.addWidget(self.meta_label)

        self.lede_label = Label("")
        self.lede_label.setWordWrap(True)
        self.lede_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        cv.addWidget(self.lede_label)

        self.callout = QtWidgets.QFrame()
        self.callout.setProperty("callout", "warning")
        wh = QtWidgets.QHBoxLayout(self.callout)
        wh.setContentsMargins(12, 8, 12, 8)
        wh.setSpacing(10)
        self.callout_label = Label("")
        # RichText: the callout is composed here and escapes each question
        self.callout_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.callout_label.setWordWrap(True)
        wh.addWidget(self.callout_label, 1)
        self.answer_now_btn = QtWidgets.QPushButton("Answer now")
        self.answer_now_btn.setToolTip(
            "Open Add answer on the Apply Answers tab with the missing question "
            "filled in; once your answer is saved, Re-queue this job")
        self.answer_now_btn.clicked.connect(on_answer_now or (lambda: None))
        wh.addWidget(self.answer_now_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        cv.addWidget(self.callout)

        self.artifacts_label = Label("")
        self.artifacts_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.artifacts_label.setWordWrap(True)
        self.artifacts_label.setProperty("muted", True)
        theme.set_type_role(self.artifacts_label, "mono")
        cv.addWidget(self.artifacts_label)

        # Their own tips, back once a single row is selected (`set_single`).
        self._own_tips = {b: b.toolTip() for b in (
            self.open_record_btn, self.open_folder_btn, self.answer_now_btn)}
        self.set_entry(None)

    def set_single(self, single: bool) -> None:
        """The card's buttons act on one job: off, with SELECT_ONE as the
        tooltip, while several rows are selected (the card still shows the
        current row)."""
        for b, tip in self._own_tips.items():
            b.setEnabled(single)
            b.setToolTip(tip if single else SELECT_ONE)

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


class _MinimumLayout(QtWidgets.QVBoxLayout):
    """The tab's column. A scroll area gives a height-for-width column its
    preferred height, so a long pause would push the queue table below the
    fold of a window with room for both at their minimum. Without it the column
    gets the window's height or its own minimum, whichever is taller."""

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt naming)
        return False


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
                 on_answer_now: Callable[[Optional[Dict[str, Any]]], Any] | None = None,
                 jev_blocked: Callable[[], str] | None = None,
                 on_check_difficulty: Callable[[List[str]], None] | None = None,
                 difficulty_blocked: Callable[[], str] | None = None,
                 difficulty_hidden: Callable[[], bool] | None = None,
                 profile_busy: Callable[[], bool] | None = None,
                 check_parallel: Callable[[], int] | None = None,
                 alert: Callable[[QtWidgets.QWidget], None] | None = None,
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
        # "Answer now" on the missing-answers callout: the main window
        # opens Add answer on the Apply Answers tab with the prefill (None when
        # the job kept no question) and returns True once the answer is saved.
        self._on_answer_now = on_answer_now or (lambda _p: None)
        # The taskbar flash for a new pause request; tests inject a spy.
        self._alert = alert or _default_alert
        self._pauses_seen: set[str] = set()
        # Late-bound like password_exists: why a run cannot start ("" when it can).
        self._jev_blocked = jev_blocked or (lambda: _default_jev_blocked())
        # The difficulty check: its console (job ids) and its
        # gates, late-bound like the others.
        self._on_check_difficulty = on_check_difficulty or _spawn_check
        self._difficulty_blocked = difficulty_blocked or (lambda: _default_difficulty_blocked())
        self._difficulty_hidden = difficulty_hidden or (lambda: _default_difficulty_hidden())
        self._profile_busy = profile_busy or (lambda: _default_profile_busy())
        # How many checks run at once, for the note only: the
        # check's console reads the setting itself.
        self._check_parallel = check_parallel or (lambda: _default_check_parallel())
        self._gate_busy = False     # the profile's state when the gates were last read
        self._jobs: List[Dict[str, Any]] = []
        self._mtime_sig: tuple | None = None
        self._build()
        self._setup_watcher()
        self.refresh()
        self._check_pauses()

    # ---- paths -----------------------------------------------------------------

    def _queue_file(self) -> Path:
        """Resolved at call time (explicit override > APPLY_QUEUE_PATH env >
        appdata default) so tests and env changes take effect immediately."""
        return apply_queue.queue_path(self._queue_override)

    # ---- construction ------------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # The tab scrolls once it outgrows the window. With a pause waiting, the
        # Jev notice and a parked job's details, it asked for 704px at 100% and
        # 889px at 150%, and a 1100x700 window squeezed its buttons to a few
        # pixels.
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll)
        self.body = QtWidgets.QWidget()
        self.scroll.setWidget(self.body)
        v = _MinimumLayout(self.body)
        v.setContentsMargins(8, 8, 8, 8)

        # A paused run's questions, above everything else on the tab.
        self.pause_card = PauseCard()
        self.pause_card.answered.connect(self._pause_answered)
        v.addWidget(self.pause_card)

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

        # Why Start is off (a test or unknown judge, or Jev unable to run), in the
        # drain's words; hidden while a run can start. Queueing goes on either way.
        self.jev_notice = QtWidgets.QFrame()
        self.jev_notice.setProperty("callout", "warning")
        jn = QtWidgets.QHBoxLayout(self.jev_notice)
        jn.setContentsMargins(12, 8, 12, 8)
        self.jev_label = Label("")
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
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
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
        for i, w in enumerate((150, 220, 150, 90, 80, 110, 150, 200)):
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
        self.details = _DetailsPanel(on_answer_now=lambda: self._answer_now())
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
                                 "Delete the selected jobs from the queue; with more "
                                 "than one selected, it asks first",
                                 tier="destructive")
        btns.addStretch(1)
        # The difficulty check. The tip is kept for
        # refresh_difficulty_state, which shows a reason in its place.
        self._check_tip = (
            "Score how hard the selected jobs (or, with none selected, every queued "
            "job) are to auto-apply, 1 to 10, in a NEW terminal window. Several jobs "
            "are checked at once, each in its own browser window on a copy of the "
            "auto-apply profile, up to the 'Difficulty checks at once' setting. Each "
            "check opens the posting, follows its Apply button and reads the first "
            "application page: it types nothing, signs in nowhere and submits "
            "nothing. About 2 to 4 Jev requests per job.")
        self.check_difficulty_btn = button("Check difficulty", self._check_difficulty,
                                           self._check_tip, tier="tertiary")
        v.addLayout(btns)
        # The buttons that act on one job: off, with SELECT_ONE as
        # the tooltip, while several rows are selected. Their own tips are kept
        # here to come back with one row.
        self._single_job_tips = {b: b.toolTip() for b in (
            self.requeue_btn, self.mark_applied_btn, self.dont_apply_btn)}

        self.status_label = Label("")
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
        """Each poll tick: a queue change reloads the panel; otherwise a
        browser opening or closing on the auto-apply profile since the gates
        were last read (`_busy_now`) reads them again, so Start comes back
        once a difficulty check's window closes. The pause folder is read on
        every tick (`_check_pauses`)."""
        self._check_pauses()
        if self._current_sig() != self._mtime_sig:
            self.refresh()
        elif self._busy_now() != self._gate_busy:
            self.refresh_jev_state()

    def _check_pauses(self) -> None:
        """The "Waiting for you" card follows the pause folder: the oldest
        waiting request shows (the one on the card stays while it waits, so a
        half-typed answer is kept), the card hides once none waits, and each
        request seen for the first time flashes the taskbar."""
        waiting = _pending_requests()
        fresh = [r for r in waiting if str(r.get("pause_id") or r.get("job") or "")
                 not in self._pauses_seen]
        for r in fresh:
            self._pauses_seen.add(str(r.get("pause_id") or r.get("job") or ""))
        current = next((r for r in waiting if self.pause_card.shows(r)), None)
        if current is None:
            self.pause_card.set_request(waiting[0] if waiting else None)
        self.pause_card.set_waiting_count(max(0, len(waiting) - 1))
        if fresh:
            try:
                self._alert(self)
            except Exception:  # noqa: BLE001 - a flash that fails is only a missed flash
                pass

    def _pause_answered(self, job_id: str, mode: str) -> None:
        words = {"fill": "Your answers went to the run",
                 "browser": "The run reads the page again",
                 "park": "The job parks"}.get(mode, "Sent")
        self._set_note(f"{words} (job {job_id}).")
        self._check_pauses()

    def _busy_now(self) -> bool:
        """The profile seam, read safely: a seam that raises reads as free
        (the drain and the check ask again as they start)."""
        try:
            return bool(self._profile_busy())
        except Exception:  # noqa: BLE001 - never break the panel
            return False

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
        selected = set(self._selected_job_ids())
        current = self._current_job_id()
        table = self.table
        table.blockSignals(True)
        try:
            table.setRowCount(len(self._jobs))
            reselect: List[int] = []
            current_row = None
            for r, e in enumerate(self._jobs):
                arts = e.get("artifacts") or {}
                missing = e.get("missing_answers") or []
                difficulty = e.get("difficulty") or {}
                cells = (
                    str(e.get("company", "")),
                    str(e.get("title", "")),
                    str(e.get("status", "")),
                    str(e.get("attempts", 0)),
                    str(len(missing) if isinstance(missing, list) else missing),
                    _difficulty_text(difficulty),
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
                        item.setToolTip(literal(str(arts.get("folder", ""))))
                    elif COLUMN_IDS[c] == "difficulty" and text:
                        item.setData(BAND_ROLE, apply_assess.band_family(
                            difficulty.get("score")))
                        item.setToolTip(_difficulty_tip(difficulty))
                    table.setItem(r, c, item)
                jid = str(e.get("job_posting_id", ""))
                if jid in selected:
                    reselect.append(r)
                if current and jid == current:
                    current_row = r
        finally:
            table.blockSignals(False)
        self._restore_selection(reselect, current_row, bool(selected))

    def _restore_selection(self, rows: List[int], current_row: Optional[int],
                           had_selection: bool) -> None:
        """Select every row whose job was selected before the refresh (the rows
        may have moved), keeping the current row on its job; a selection whose
        jobs all left the queue clears."""
        sm = self.table.selectionModel()
        if sm is None or not had_selection:
            return
        model = self.table.model()
        flags = QtCore.QItemSelectionModel.SelectionFlag
        if current_row is not None:
            sm.setCurrentIndex(model.index(current_row, 0), flags.NoUpdate)
        if not rows:
            self.table.clearSelection()
            return
        last = model.columnCount() - 1
        selection = QtCore.QItemSelection()
        for r in rows:
            selection.select(model.index(r, 0), model.index(r, last))
        sm.select(selection, flags.ClearAndSelect | flags.Rows)

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

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Read the Jev gate again each time the tab comes into view: the user
        may have fixed the named problem (installed the SDK, turned Jev on in
        config.json) since the last refresh, and Start is out of reach while
        it is off."""
        super().showEvent(event)
        self.refresh_jev_state()

    def event(self, event) -> bool:
        """...and each time the dashboard window comes back to the front while
        this tab shows, which is how a fix made in a terminal (pip install)
        gets back here. Qt hands WindowActivate down to the visible children
        only, so a hidden tab reads nothing."""
        if event.type() == QtCore.QEvent.Type.WindowActivate:
            self.refresh_jev_state()
        return super().event(event)

    def refresh_jev_state(self) -> str:
        """Start is off, with the reason as its tooltip and in the notice under
        the buttons, while the drain it launches would refuse to start (a test
        judge, or Jev unable to run; then another browser on the
        auto-apply profile, a difficulty check or a sign-in:
        `profile_lock.RUN_BUSY`). Read on every refresh, after a Settings
        save (the main window calls this), whenever the tab shows or the
        window comes back to the front while it shows, at each Start click,
        and on the poll tick after the profile's state moves. Returns the
        reason, "" when a run can start."""
        self._gate_busy = self._busy_now()
        try:
            reason = str(self._jev_blocked() or "")
        except Exception:  # noqa: BLE001 - the drain checks again; never break the panel
            reason = ""
        if not reason and self._gate_busy:
            reason = profile_lock.RUN_BUSY
        self.start_run_btn.setEnabled(not reason)
        self.start_run_btn.setToolTip(literal(reason or self._start_tip))
        self.jev_label.setText(reason)
        self.jev_notice.setVisible(bool(reason))
        self.refresh_difficulty_state()
        return reason

    def _difficulty_gate(self) -> tuple[bool, str, bool]:
        """(hidden, why the check cannot run, a browser holds the profile),
        each read from its seam; a seam that raises reads as no bar (the
        check asks again as it starts)."""
        try:
            hidden = bool(self._difficulty_hidden())
        except Exception:  # noqa: BLE001 - never break the panel
            hidden = False
        try:
            reason = "" if hidden else str(self._difficulty_blocked() or "")
        except Exception:  # noqa: BLE001
            reason = ""
        try:
            busy = False if hidden else bool(self._profile_busy())
        except Exception:  # noqa: BLE001
            busy = False
        return hidden, reason, busy

    def refresh_difficulty_state(self) -> tuple[str, bool]:
        """The Check difficulty button: hidden while the check is
        switched off; off with the check's reason as its tooltip while Jev
        cannot run or a browser holds the auto-apply profile. Read with the
        Jev gate (`refresh_jev_state`) and on each selection. Returns (the
        reason, busy)."""
        hidden, reason, busy = self._difficulty_gate()
        self.check_difficulty_btn.setVisible(not hidden)
        self.check_difficulty_btn.setEnabled(not reason and not busy)
        self.check_difficulty_btn.setToolTip(
            literal(reason or (apply_assess.PROFILE_BUSY if busy else self._check_tip)))
        return reason, busy

    # ---- selection / details --------------------------------------------------------

    def _row_job_id(self, row: int) -> str:
        item = self.table.item(row, 0)
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "") if item else ""

    def _selected_rows(self) -> List[int]:
        sm = self.table.selectionModel()
        return sorted(i.row() for i in sm.selectedRows()) if sm else []

    def _selected_job_ids(self) -> List[str]:
        """Every selected job's id, in table order."""
        return [jid for jid in (self._row_job_id(r) for r in self._selected_rows()) if jid]

    def _selected_job_id(self) -> str:
        """The selected job's id, "" unless exactly one row is selected (the
        single-job actions have no one job to act on otherwise)."""
        ids = self._selected_job_ids()
        return ids[0] if len(ids) == 1 else ""

    def _entry_for(self, jid: str) -> Optional[Dict[str, Any]]:
        if not jid:
            return None
        for e in self._jobs:
            if str(e.get("job_posting_id", "")) == jid:
                return e
        return None

    def _selected_entry(self) -> Optional[Dict[str, Any]]:
        return self._entry_for(self._selected_job_id())

    def _current_job_id(self) -> str:
        """The row the details pane shows: the current row while it is
        selected, else the first selected row; "" with nothing selected."""
        rows = self._selected_rows()
        if not rows:
            return ""
        current = self.table.currentRow()
        return self._row_job_id(current if current in rows else rows[0])

    def _update_details(self) -> None:
        single = len(self._selected_rows()) <= 1
        self.details.set_entry(self._entry_for(self._current_job_id()))
        self.details.set_single(single)
        for b, tip in self._single_job_tips.items():
            b.setEnabled(single)
            b.setToolTip(tip if single else SELECT_ONE)
        self.refresh_difficulty_state()

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
        self._requeue_job(jid)

    def _requeue_job(self, jid: str) -> None:
        qp = self._queue_file()
        self._submit_write(
            lambda: apply_queue.requeue(jid, refresh_answers=True, path=qp),
            on_done=lambda _r: self.refresh(), on_error=self._write_failed)

    def _remove(self) -> None:
        """Remove the selected job; several selected rows go in one write after
        a confirm (`_confirm_remove`)."""
        ids = self._selected_job_ids()
        if len(ids) > 1:
            self._remove_many(ids)
            return
        jid = self._selected_job_id()
        if not jid:
            self._set_note("Select a row to remove.")
            return
        qp = self._queue_file()
        self._submit_write(lambda: apply_queue.remove(jid, path=qp),
                           on_done=lambda _r: self.refresh(),
                           on_error=self._write_failed)

    def _remove_many(self, ids: List[str]) -> None:
        if not self._confirm_remove(len(ids)):
            return
        qp = self._queue_file()

        def done(removed: Any) -> None:
            self.refresh()
            self._set_note(f"Removed {removed} job(s) from the queue.")

        self._submit_write(lambda: apply_queue.remove_many(ids, path=qp),
                           on_done=done, on_error=self._write_failed)

    def _confirm_remove(self, n: int) -> bool:
        """Ask before removing several jobs; tests monkeypatch this."""
        answer = QtWidgets.QMessageBox.question(
            self, "Remove from queue",
            literal(f"Remove the {n} selected jobs from the auto-apply queue? Their job "
            f"folders stay on disk."))
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

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
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setWindowTitle("Start auto-apply run")
        box.setText(self._confirm_text(n))
        start_btn = box.addButton("Start", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(start_btn)
        box.exec()
        return box.clickedButton() is start_btn

    def _confirm_check(self, n: int) -> bool:
        """Ask before checking every queued job; tests monkeypatch this."""
        answer = QtWidgets.QMessageBox.question(
            self, "Check difficulty",
            literal(f"Check how hard each of the {n} queued job(s) is to auto-apply? A new "
            f"terminal reads each job's first application page in the auto-apply "
            f"browser, {self._at_once_words(n)}: about 2 to 4 Jev requests per job."))
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def _parallel_now(self) -> int:
        """The checks-at-once seam, read safely and clamped 1 to 10."""
        try:
            return assess_pool.parallel_setting(self._check_parallel())
        except Exception:  # noqa: BLE001 - a config hiccup only changes a note
            return assess_pool.PARALLEL_DEFAULT

    def _at_once_words(self, n: int) -> str:
        """How `n` jobs are checked: "up to K at once" (K never more than n),
        or "one at a time"."""
        k = min(self._parallel_now(), n)
        return f"up to {k} at once" if k > 1 else "one at a time"

    def _check_difficulty(self) -> None:
        """Check the selected jobs (several run at once, up to the
        `auto_apply_check_parallel` setting, which the check's console reads),
        or every queued job after a confirm. The gate is read again first: a
        run may have taken the profile since."""
        reason, busy = self.refresh_difficulty_state()
        if reason or busy:
            self._set_note(reason or apply_assess.PROFILE_BUSY)
            return
        ids = self._selected_job_ids()
        if len(ids) == 1:
            if self._launch_check(ids):
                self._set_note("Checking the selected job's difficulty in a new terminal.")
            return
        if ids:
            # every queued job selected: `--all` keeps the command line short
            # (Windows caps it near 32,767 characters, about 900 ids)
            queued_ids = {str(e.get("job_posting_id") or "") for e in self._jobs
                          if e.get("status") == "queued"}
            if self._launch_check([] if set(ids) == queued_ids else ids):
                self._set_note(f"Checking {len(ids)} selected jobs, "
                               f"{self._at_once_words(len(ids))}, in a new terminal.")
            return
        queued = self._queued_count()
        if queued == 0:
            self._set_note("Select a job, or queue jobs to check.")
            return
        if not self._confirm_check(queued):
            return
        if self._launch_check([]):
            self._set_note(f"Checking {queued} queued job(s) in a new terminal.")

    def _launch_check(self, ids: List[str]) -> bool:
        """Start the check's console for `ids` (every queued job when empty);
        False, with a note, when it will not start (a command line too long
        for Windows among the reasons)."""
        try:
            self._on_check_difficulty(ids)
        except ValueError:
            self._set_note("The difficulty check did not start: a selected job's id "
                           "holds characters the check cannot pass on.")
            return False
        except OSError as e:
            more = (" Select fewer jobs, or select none to check every queued job."
                    if len(ids) > 1 else "")
            self._set_note(f"The difficulty check did not start ({type(e).__name__}).{more}")
            return False
        return True

    def _missing_questions(self) -> List[Dict[str, Any]]:
        """The selected job's missing answers that name a question."""
        return [m for m in (self._selected_entry() or {}).get("missing_answers") or []
                if isinstance(m, dict) and str(m.get("question") or "").strip()]

    def _answer_menu(self) -> QtWidgets.QMenu:
        """One entry per missing question of the selected job."""
        menu = QtWidgets.QMenu(self)
        for m in self._missing_questions():
            action = menu.addAction(str(m.get("question") or ""))
            action.triggered.connect(
                lambda _c=False, m=m: self._answer_with(_missing_prefill(m)))
        return menu

    def _answer_now(self) -> None:
        """Answer now: Add answer prefilled with the parked question, or
        a menu of them when there are several; a job with none opens the
        Apply Answers tab."""
        questions = self._missing_questions()
        if not questions:
            self._on_answer_now(None)
            return
        if len(questions) == 1:
            self._answer_with(_missing_prefill(questions[0]))
            return
        menu = self._answer_menu()
        btn = self.details.answer_now_btn
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _answer_with(self, prefill: Dict[str, Any]) -> None:
        """The answer saved, a Re-queue is offered only for a job parked or
        failed with nothing sent: never for a submitted job,
        one waiting for the person's submit, or one that may have been sent."""
        e = self._selected_entry()
        jid = str((e or {}).get("job_posting_id") or "")
        saved = self._on_answer_now(prefill)
        offer = (str((e or {}).get("status") or "") in ("needs_human", "failed")
                 and not apply_queue.possibly_sent(e or {}))
        if saved is True and jid and offer and self._confirm_requeue(e or {}):
            self._requeue_job(jid)
            self._set_note(f"Re-queued {(e or {}).get('company', '')}: the next run uses "
                           f"your answer.")

    def _confirm_requeue(self, e: Dict[str, Any]) -> bool:
        """Ask to Re-queue once the answer is saved; tests monkeypatch this."""
        answer = QtWidgets.QMessageBox.question(
            self, "Re-queue",
            literal(f"Your answer is saved. Re-queue {e.get('company', 'this job')} so the next "
            f"run applies with it?"))
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def _copy_kickoff(self) -> None:
        QtWidgets.QApplication.clipboard().setText(KICKOFF_COMMAND)
        self._set_note("Kickoff command copied to the clipboard.")

    def _sign_in(self) -> None:
        try:
            self._on_login()
        except OSError as e:     # no console could start (PowerShell missing, say)
            self._set_note(f"The sign-in did not start: {errmsg.for_user(e)}")
            return
        self._set_note("Sign in to LinkedIn and your inbox in the new browser window, "
                       "then close it.")

    def _start_run(self) -> None:
        """Guards, in order: Jev can run and no browser holds the profile ->
        password set -> queue non-empty -> confirm. Only a confirmed dialog calls the injected on_start_run
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
        try:
            self._on_start_run()
        except OSError as e:     # no console could start (PowerShell missing, say)
            self._set_note(f"The auto-apply run did not start: {errmsg.for_user(e)}")
            return
        self._set_note("Auto-apply run started in a new terminal window.")
