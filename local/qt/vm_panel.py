"""VM operations panel (Qt): schedule editor, pause-until, and push to the cloud
scraper VM via gcloud.

Mounted inside the Settings VM section. Every side-effectful action confirms first
and runs through an injectable `runner` (default `vm_sync.run_cmd`), so tests drive
it without touching a real VM. No secret is stored — connection identifiers come
from .env via settings; auth is the user's existing `gcloud` login.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from PySide6 import QtCore, QtWidgets

import jobsdata
import local_task
import settings
import vm_schedule
import vm_sync

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
BLANK = "—"
HOUR_OPTIONS = [f"{h:02d}:00" for h in range(24)]
MAX_TIMES = vm_schedule.MAX_TIMES_PER_DAY


class VMPanel(QtWidgets.QWidget):
    def __init__(self, targets: dict | None = None, runner: Callable | None = None,
                 confirm: Callable | None = None, notify: Callable | None = None,
                 target_factory: Callable | None = None, parent=None):
        super().__init__(parent)
        self.targets = targets
        self._runner = runner or vm_sync.run_cmd
        self._confirm = confirm or (
            lambda title, msg: QtWidgets.QMessageBox.question(self, title, msg)
            == QtWidgets.QMessageBox.StandardButton.Yes)
        self._notify = notify or (
            lambda title, msg: QtWidgets.QMessageBox.information(self, title, msg))
        self._target_factory = target_factory or (lambda: vm_sync.VMTarget.from_env(self.targets))

        self.time_combos: list[QtWidgets.QComboBox] = []
        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        head = QtWidgets.QLabel("Cloud job-discovery VM")
        head.setProperty("heading", True)
        v.addWidget(head)
        t = self._target_factory()
        status = (f"Connected target: {t.user}@{t.instance} (zone {t.zone})" if t.configured()
                  else "No VM configured — set VM_INSTANCE / VM_ZONE / VM_USER in Settings.")
        self.status_label = QtWidgets.QLabel(status)
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)

        # --- schedule ---
        v.addWidget(QtWidgets.QLabel("Schedule — pick up to 6 run times (>=2h apart):"))
        grid = QtWidgets.QGridLayout()
        for i in range(MAX_TIMES):
            cell = QtWidgets.QHBoxLayout()
            cell.addWidget(QtWidgets.QLabel(f"Run {i + 1}"))
            combo = QtWidgets.QComboBox()
            combo.addItems([BLANK, *HOUR_OPTIONS])
            combo.currentIndexChanged.connect(self._refresh_preview)
            self.time_combos.append(combo)
            cell.addWidget(combo)
            grid.addLayout(cell, i % 3, i // 3)
        v.addLayout(grid)

        freq_row = QtWidgets.QHBoxLayout()
        freq_row.addWidget(QtWidgets.QLabel("Frequency"))
        self.freq = QtWidgets.QComboBox()
        self.freq.addItems(list(vm_schedule.FREQS))
        self.freq.currentIndexChanged.connect(self._on_freq_changed)
        freq_row.addWidget(self.freq)
        freq_row.addWidget(QtWidgets.QLabel("Weekday"))
        self.weekday = QtWidgets.QComboBox()
        self.weekday.addItems(list(WEEKDAYS))
        self.weekday.setCurrentText("Monday")
        self.weekday.currentIndexChanged.connect(self._refresh_preview)
        freq_row.addWidget(self.weekday)
        freq_row.addStretch(1)
        v.addLayout(freq_row)

        v.addWidget(QtWidgets.QLabel("crontab preview:"))
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(90)
        v.addWidget(self.preview)

        sbar = QtWidgets.QHBoxLayout()
        validate = QtWidgets.QPushButton("Validate")
        validate.clicked.connect(self._validate_only)
        sbar.addWidget(validate)
        apply_btn = QtWidgets.QPushButton("Apply schedule to VM")
        apply_btn.setProperty("accent", True)
        apply_btn.clicked.connect(self.apply_schedule)
        sbar.addWidget(apply_btn)
        sbar.addStretch(1)
        v.addLayout(sbar)

        # --- pause ---
        pause_head = QtWidgets.QLabel("Pause")
        pause_head.setProperty("heading", True)
        v.addWidget(pause_head)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("Until"))
        self.pause_date = QtWidgets.QDateEdit()
        self.pause_date.setCalendarPopup(True)
        tomorrow = date.today() + timedelta(days=1)
        self.pause_date.setMinimumDate(QtCore.QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        self.pause_date.setDate(QtCore.QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        prow.addWidget(self.pause_date)
        prow.addWidget(QtWidgets.QLabel("time"))
        self.pause_time = QtWidgets.QComboBox()
        self.pause_time.addItems([BLANK, *HOUR_OPTIONS])
        prow.addWidget(self.pause_time)
        pause_btn = QtWidgets.QPushButton("Pause VM")
        pause_btn.clicked.connect(self.pause)
        prow.addWidget(pause_btn)
        resume_btn = QtWidgets.QPushButton("Resume now")
        resume_btn.clicked.connect(self.resume)
        prow.addWidget(resume_btn)
        prow.addStretch(1)
        v.addLayout(prow)

        # --- push ---
        push_head = QtWidgets.QLabel("Push")
        push_head.setProperty("heading", True)
        v.addWidget(push_head)
        note = QtWidgets.QLabel("Copy your current search + scoring config up to the VM.")
        note.setProperty("muted", True)
        v.addWidget(note)
        push_btn = QtWidgets.QPushButton("Push config to VM")
        push_btn.clicked.connect(self.push_config)
        v.addWidget(push_btn, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)

        # --- local watcher task ---
        lt_head = QtWidgets.QLabel("Local watcher task")
        lt_head.setProperty("heading", True)
        v.addWidget(lt_head)
        lt_note = QtWidgets.QLabel(
            "The LinkedInJobsWatcher scheduled task checks for fresh results a few "
            "minutes after each VM run (offsets in Settings above).")
        lt_note.setProperty("muted", True)
        lt_note.setWordWrap(True)
        v.addWidget(lt_note)
        lt_row = QtWidgets.QHBoxLayout()
        self.sync_local_btn = QtWidgets.QPushButton("Sync local task now")
        self.sync_local_btn.clicked.connect(self.sync_local_task)
        lt_row.addWidget(self.sync_local_btn)
        self.restore_local_btn = QtWidgets.QPushButton("Restore default local schedule")
        self.restore_local_btn.clicked.connect(self.restore_local_task)
        lt_row.addWidget(self.restore_local_btn)
        lt_row.addStretch(1)
        v.addLayout(lt_row)

        self.set_times(self._seed_times())
        self._sync_weekday_enabled()

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _seed_times() -> list[str]:
        """Last times pushed to the VM (the panel never reads live VM state), or
        the stock 10:00/19:00 pair before anything has ever been pushed."""
        return jobsdata.load_vm_schedule_times() or ["10:00", "19:00"]

    def set_times(self, times: list[str]) -> None:
        for i, combo in enumerate(self.time_combos):
            combo.setCurrentText(times[i] if i < len(times) else BLANK)
        self._refresh_preview()

    def revert(self) -> None:
        """Reset the schedule and pause editors to their initial state.

        The Settings form's 'Revert changes' calls this so the VM section rolls
        back alongside the rest of the form. The panel never loads live VM state,
        so 'initial' is what it constructed with — the last-pushed times (or the
        stock pair before anything was ever pushed).
        """
        self.freq.setCurrentIndex(0)
        self.weekday.setCurrentText("Monday")
        self.pause_time.setCurrentText(BLANK)
        tomorrow = date.today() + timedelta(days=1)
        self.pause_date.setDate(QtCore.QDate(tomorrow.year, tomorrow.month, tomorrow.day))
        self.set_times(self._seed_times())  # also refreshes the crontab preview
        self._sync_weekday_enabled()

    def _times(self) -> list[str]:
        return [c.currentText() for c in self.time_combos
                if c.currentText() and c.currentText() != BLANK]

    def _weekday_idx(self) -> int:
        try:
            return WEEKDAYS.index(self.weekday.currentText())
        except ValueError:
            return 1

    def crontab_text(self) -> str:
        return vm_schedule.build_crontab(self._times(), freq=self.freq.currentText(),
                                         weekday=self._weekday_idx())

    def _on_freq_changed(self, *_):
        self._sync_weekday_enabled()
        self._refresh_preview()

    def _sync_weekday_enabled(self):
        self.weekday.setEnabled(self.freq.currentText() in ("weekly", "biweekly"))

    def _refresh_preview(self, *_):
        if not hasattr(self, "preview"):
            return
        try:
            text = self.crontab_text()
        except Exception:  # noqa: BLE001 - cosmetic
            text = ""
        self.preview.setPlainText(text)

    def _require_configured(self):
        t = self._target_factory()
        if not t.configured():
            self._notify("VM", "No VM configured. Set VM_INSTANCE / VM_ZONE / VM_USER in Settings.")
            return None
        return t

    def _run_result(self, cmd: list[str]) -> tuple[bool, str]:
        """Run `cmd` through the injectable runner; (ok, notify-ready text).
        Doesn't notify itself, so a caller can fold follow-up work into one popup."""
        try:
            res = self._runner(cmd)
        except Exception as exc:  # noqa: BLE001
            return False, f"Command failed to launch: {exc}"
        out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        ok = getattr(res, "returncode", 0) == 0
        return ok, ("Done.\n\n" if ok else "Failed.\n\n") + out[:1200]

    def _run(self, cmd: list[str]) -> bool:
        ok, text = self._run_result(cmd)
        self._notify("VM", text)
        return ok

    # ---- actions -------------------------------------------------------------

    def _validate_only(self):
        errs = vm_schedule.validate_schedule(self._times(), self.freq.currentText())
        self._notify("Schedule", "\n".join(errs) if errs else "Schedule looks good.")

    def apply_schedule(self):
        errs = vm_schedule.validate_schedule(self._times(), self.freq.currentText())
        if errs:
            self._notify("Schedule", "\n".join(errs))
            return
        t = self._require_configured()
        if not t:
            return
        cron = self.crontab_text()
        if not self._confirm("Apply schedule", f"Replace the VM crontab with:\n\n{cron}\n\nProceed?"):
            return
        ok, text = self._run_result(t.install_crontab_cmd(cron))
        if ok:
            times = self._times()
            jobsdata.save_vm_schedule_times(times)   # seeds the editor next open
            if bool(jobsdata._load_cfg().get("local_task_autosync", False)):
                wok, wmsg = local_task.register(
                    local_task.watcher_times(times, self._cfg_offsets()))
                text += (("\n\nLocal watcher task synced too.\n" if wok
                          else "\n\nBUT the local watcher task sync failed:\n")
                         + wmsg[:400])
        self._notify("VM", text)

    def pause(self):
        t = self._require_configured()
        if not t:
            return
        pause_date = self.pause_date.date().toString("yyyy-MM-dd")
        time = self.pause_time.currentText()
        if time == BLANK:
            time = ""
        val = vm_schedule.pause_until_value(pause_date, time)
        if not self._confirm("Pause VM", f"Pause job discovery until {val}?"):
            return
        self._run(t.set_pause_cmd(val))

    def resume(self):
        t = self._require_configured()
        if not t:
            return
        if not self._confirm("Resume VM", "Remove the pause and resume the schedule?"):
            return
        self._run(t.resume_cmd())

    def push_config(self):
        t = self._require_configured()
        if not t:
            return
        files = [(settings.TARGET_FILES[k], remote)
                 for k, remote in vm_sync.TARGET_REMOTE_FILE.items()]
        names = ", ".join(remote for _, remote in files)
        if not self._confirm("Push config", f"Copy {names} to the VM?"):
            return
        for local, remote in files:
            self._run(t.build_scp_cmd(str(local), remote))

    # ---- local watcher task ----------------------------------------------------

    @staticmethod
    def _cfg_offsets() -> tuple[int, ...]:
        """The watcher-check offsets from settings ('30,50,70'-style, junk-safe)."""
        return local_task.parse_offsets(
            jobsdata._load_cfg().get("local_task_offsets", "30,50,70"))

    def sync_local_task(self):
        """Re-register the local watcher task from the CURRENT combo times — the
        manual fix for a stale schedule, no VM push (or auto-sync) required."""
        times = self._times()
        if not times:
            self._notify("Local watcher task", "Pick at least one run time first.")
            return
        watcher = local_task.watcher_times(times, self._cfg_offsets())
        if not self._confirm(
                "Sync local task",
                f"Re-register the local '{local_task.TASK_NAME}' task to check for "
                f"results at:\n\n{', '.join(watcher)}\n\nProceed?"):
            return
        ok, msg = local_task.register(watcher)
        self._notify("Local watcher task",
                     ("Synced.\n\n" if ok else "Failed.\n\n") + msg[:1200])

    def restore_local_task(self):
        """Escape hatch: put the task back on its stock schedule and turn
        auto-sync off, so nothing moves it again until the user opts back in."""
        if not self._confirm(
                "Restore default schedule",
                f"Re-register '{local_task.TASK_NAME}' with its default times "
                f"({', '.join(local_task.DEFAULT_TIMES)}) and turn auto-sync off?"):
            return
        ok, msg = local_task.register(list(local_task.DEFAULT_TIMES))
        jobsdata._save_cfg({"local_task_autosync": False})
        self._notify("Local watcher task",
                     ("Default schedule restored; auto-sync is now off.\n\n" if ok
                      else "Failed.\n\n") + msg[:1200])
