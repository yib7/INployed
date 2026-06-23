"""Dashboard 'VM' tab: schedule editor, pause-until, and one-click push to the
cloud scraper VM via gcloud.

Theme-agnostic (reads colors from the active ttk style; never imports ui). Every
side-effectful action confirms first and runs through an injectable `runner`
(default vm_sync.run_cmd), so tests drive it without ever touching a real VM.
Nothing here stores a secret — connection identifiers come from the .env via
settings; auth is the user's existing `gcloud` login.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

import settings
import vm_schedule
import vm_sync

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class VMPanel:
    def __init__(self, parent: tk.Widget, targets: dict | None = None,
                 runner: Callable | None = None, confirm: Callable | None = None,
                 notify: Callable | None = None, target_factory: Callable | None = None):
        self.parent = parent
        self.targets = targets
        self._runner = runner or vm_sync.run_cmd
        self._confirm = confirm or (
            lambda title, msg: messagebox.askyesno(title, msg, parent=parent.winfo_toplevel()))
        self._notify = notify or (
            lambda title, msg: messagebox.showinfo(title, msg, parent=parent.winfo_toplevel()))
        self._target_factory = target_factory or (
            lambda: vm_sync.VMTarget.from_env(self.targets))

        style = ttk.Style(parent)
        self._font = style.lookup("TLabel", "font") or "Segoe UI 10"
        self._field_bg = style.lookup("TEntry", "fieldbackground") or "#0f1420"
        self._fg = style.lookup("TLabel", "foreground") or "#e6e9ef"

        self.frame = ttk.Frame(parent, padding=(16, 12))
        self.freq_var = tk.StringVar(value="daily")
        self.weekday_var = tk.StringVar(value="Monday")
        self.pause_date_var = tk.StringVar(value="")
        self.pause_time_var = tk.StringVar(value="")
        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        f = self.frame
        t = self._target_factory()
        status = (f"Connected target: {t.user}@{t.instance} (zone {t.zone})"
                  if t.configured()
                  else "No VM configured — set VM_INSTANCE / VM_ZONE / VM_USER in Settings.")
        ttk.Label(f, text="Cloud scraper VM", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text=status, style="Muted.TLabel", wraplength=640).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # --- Schedule ---------------------------------------------------------
        ttk.Label(f, text="Schedule", style="Subtitle.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(f, text="Run times (one HH:MM per line, max 6, ≥2h apart)").grid(
            row=3, column=0, sticky="nw", padx=(0, 10))
        self.times_txt = tk.Text(f, width=12, height=6, wrap="none", font=self._font,
                                 bg=self._field_bg, fg=self._fg, insertbackground=self._fg,
                                 relief="flat", highlightthickness=1, highlightbackground="#2a3344")
        self.times_txt.insert("1.0", "10:00\n19:00")
        self.times_txt.grid(row=3, column=1, sticky="w")

        freq_box = ttk.Frame(f)
        freq_box.grid(row=3, column=2, sticky="nw", padx=(12, 0))
        ttk.Label(freq_box, text="Frequency").grid(row=0, column=0, sticky="w")
        ttk.Combobox(freq_box, textvariable=self.freq_var, state="readonly", width=12,
                     values=list(vm_schedule.FREQS)).grid(row=1, column=0, sticky="w")
        ttk.Label(freq_box, text="Weekday (weekly/biweekly)").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(freq_box, textvariable=self.weekday_var, state="readonly", width=12,
                     values=list(WEEKDAYS)).grid(row=3, column=0, sticky="w")

        ttk.Label(f, text="crontab preview").grid(row=4, column=0, sticky="nw", padx=(0, 10),
                                                  pady=(10, 0))
        self.preview = tk.Text(f, width=64, height=4, wrap="none", font=self._font,
                               bg=self._field_bg, fg=self._fg, relief="flat",
                               highlightthickness=1, highlightbackground="#2a3344")
        self.preview.grid(row=4, column=1, columnspan=2, sticky="w", pady=(10, 0))
        self._refresh_preview()
        for w in (self.times_txt,):
            w.bind("<KeyRelease>", lambda _e: self._refresh_preview())
        self.freq_var.trace_add("write", lambda *_: self._refresh_preview())

        sbar = ttk.Frame(f)
        sbar.grid(row=5, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(sbar, text="Validate", command=self._validate_only).pack(side="left")
        ttk.Button(sbar, text="Apply schedule to VM", style="Accent.TButton",
                   command=self.apply_schedule).pack(side="left", padx=(8, 0))

        # --- Pause ------------------------------------------------------------
        ttk.Label(f, text="Pause", style="Subtitle.TLabel").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(16, 2))
        pbox = ttk.Frame(f)
        pbox.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(pbox, text="Until date (YYYY-MM-DD)").pack(side="left")
        ttk.Entry(pbox, textvariable=self.pause_date_var, width=14).pack(side="left", padx=(6, 12))
        ttk.Label(pbox, text="time (HH:MM, optional)").pack(side="left")
        ttk.Entry(pbox, textvariable=self.pause_time_var, width=8).pack(side="left", padx=(6, 12))
        ttk.Button(pbox, text="Pause VM", command=self.pause).pack(side="left")
        ttk.Button(pbox, text="Resume now", command=self.resume).pack(side="left", padx=(8, 0))

        # --- Push -------------------------------------------------------------
        ttk.Label(f, text="Push", style="Subtitle.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(16, 2))
        ttk.Label(f, text="Copy your current search + scoring config up to the VM.",
                  style="Muted.TLabel").grid(row=9, column=0, columnspan=3, sticky="w")
        ttk.Button(f, text="Push config to VM", command=self.push_config).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # ---- helpers -------------------------------------------------------------

    def set_times(self, times: list[str]) -> None:
        self.times_txt.delete("1.0", "end")
        self.times_txt.insert("1.0", "\n".join(times))
        self._refresh_preview()

    def set_pause_inputs(self, date: str, time: str = "") -> None:
        self.pause_date_var.set(date)
        self.pause_time_var.set(time)

    def _times(self) -> list[str]:
        return [ln.strip() for ln in self.times_txt.get("1.0", "end").splitlines() if ln.strip()]

    def _weekday_idx(self) -> int:
        try:
            return WEEKDAYS.index(self.weekday_var.get())
        except ValueError:
            return 1

    def crontab_text(self) -> str:
        return vm_schedule.build_crontab(self._times(), freq=self.freq_var.get(),
                                         weekday=self._weekday_idx())

    def _refresh_preview(self) -> None:
        try:
            text = self.crontab_text()
        except Exception:  # noqa: BLE001 - preview is cosmetic
            text = ""
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)

    def _require_configured(self):
        t = self._target_factory()
        if not t.configured():
            self._notify("VM", "No VM configured. Set VM_INSTANCE / VM_ZONE / VM_USER in Settings.")
            return None
        return t

    def _run(self, cmd: list[str]) -> None:
        try:
            res = self._runner(cmd)
        except Exception as exc:  # noqa: BLE001
            self._notify("VM", f"Command failed to launch: {exc}")
            return
        out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        ok = getattr(res, "returncode", 0) == 0
        self._notify("VM", ("Done.\n\n" if ok else "Failed.\n\n") + out[:1200])

    # ---- actions -------------------------------------------------------------

    def _validate_only(self) -> None:
        errs = vm_schedule.validate_schedule(self._times(), self.freq_var.get())
        self._notify("Schedule", "\n".join(errs) if errs else "Schedule looks good.")

    def apply_schedule(self) -> None:
        errs = vm_schedule.validate_schedule(self._times(), self.freq_var.get())
        if errs:
            self._notify("Schedule", "\n".join(errs))
            return
        t = self._require_configured()
        if not t:
            return
        cron = self.crontab_text()
        if not self._confirm("Apply schedule",
                             f"Replace the VM crontab with:\n\n{cron}\n\nProceed?"):
            return
        self._run(t.install_crontab_cmd(cron))

    def pause(self) -> None:
        date = self.pause_date_var.get().strip()
        if not date:
            self._notify("Pause", "Enter an 'until' date (YYYY-MM-DD).")
            return
        t = self._require_configured()
        if not t:
            return
        val = vm_schedule.pause_until_value(date, self.pause_time_var.get())
        if not self._confirm("Pause VM", f"Pause the scraper until {val}?"):
            return
        self._run(t.set_pause_cmd(val))

    def resume(self) -> None:
        t = self._require_configured()
        if not t:
            return
        if not self._confirm("Resume VM", "Remove the pause and resume the schedule?"):
            return
        self._run(t.resume_cmd())

    def push_config(self) -> None:
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
