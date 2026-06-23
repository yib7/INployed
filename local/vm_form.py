"""Dashboard 'VM' tab: schedule editor, pause-until, and one-click push to the
cloud scraper VM via gcloud.

Theme-agnostic (reads colors from the active ttk style; never imports ui). Every
side-effectful action confirms first and runs through an injectable `runner`
(default vm_sync.run_cmd), so tests drive it without ever touching a real VM.
Nothing here stores a secret — connection identifiers come from the .env via
settings; auth is the user's existing `gcloud` login.
"""
from __future__ import annotations

import calendar as _calendar
import tkinter as tk
from datetime import date, timedelta
from tkinter import messagebox, ttk
from typing import Callable

import settings
import vm_schedule
import vm_sync

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
BLANK = "—"  # "no time picked" sentinel in the hour dropdowns
HOUR_OPTIONS = [f"{h:02d}:00" for h in range(24)]
MAX_TIMES = vm_schedule.MAX_TIMES_PER_DAY


def is_future(d: date, today: date | None = None) -> bool:
    """True only for days strictly after today — today and the past can't be a
    meaningful 'pause until' (pausing until today is a no-op)."""
    return d > (today or date.today())


class DatePicker:
    """A small stdlib month calendar popup. Today and earlier are disabled
    (greyed); clicking a future day calls on_pick(iso) and closes."""

    def __init__(self, parent: tk.Widget, on_pick: Callable[[str], None],
                 initial: date | None = None):
        self.on_pick = on_pick
        self._today = date.today()
        self.cur = (initial or self._today).replace(day=1)
        self.day_buttons: dict[date, ttk.Button] = {}
        self.win = tk.Toplevel(parent)
        self.win.title("Pick a date")
        self.win.transient(parent.winfo_toplevel())
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        self._build()

    def _build(self) -> None:
        hdr = ttk.Frame(self.win)
        hdr.grid(row=0, column=0, columnspan=7, sticky="ew", padx=6, pady=6)
        ttk.Button(hdr, text="◀", width=3, command=self._prev).pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Label(hdr, textvariable=self.title_var, anchor="center").pack(
            side="left", expand=True, fill="x")
        ttk.Button(hdr, text="▶", width=3, command=self._next).pack(side="left")
        for i, wd in enumerate(("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")):
            ttk.Label(self.win, text=wd, anchor="center", width=4).grid(
                row=1, column=i, padx=1, pady=1)
        self._render()

    def _render(self) -> None:
        for b in self.day_buttons.values():
            b.destroy()
        self.day_buttons.clear()
        self.title_var.set(self.cur.strftime("%B %Y"))
        cal = _calendar.Calendar(firstweekday=6)  # Sunday-first to match headers
        row = 2
        for week in cal.monthdatescalendar(self.cur.year, self.cur.month):
            for col, d in enumerate(week):
                if d.month != self.cur.month:
                    continue  # blank cell for adjacent-month days
                state = "normal" if is_future(d, self._today) else "disabled"
                b = ttk.Button(self.win, text=str(d.day), width=4, state=state,
                               command=lambda dd=d: self._choose(dd))
                b.grid(row=row, column=col, padx=1, pady=1)
                self.day_buttons[d] = b
            row += 1

    def _choose(self, d: date) -> None:
        self.on_pick(d.isoformat())
        self.win.destroy()

    def _prev(self) -> None:
        self.cur = (self.cur - timedelta(days=1)).replace(day=1)
        self._render()

    def _next(self) -> None:
        y, m = self.cur.year, self.cur.month
        self.cur = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        self._render()


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
        self.pause_time_var = tk.StringVar(value=BLANK)
        self.time_vars: list[tk.StringVar] = []
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
        ttk.Label(f, text="Run times (pick up to 6, ≥2h apart)").grid(
            row=3, column=0, sticky="nw", padx=(0, 10))
        times_box = ttk.Frame(f)
        times_box.grid(row=3, column=1, sticky="w")
        self.times_box = times_box
        # Six clearly-numbered slots (Run 1..6); each picked time becomes its own
        # crontab line, and the preview is fully rebuilt on every change (never
        # appended). Laid out 3 rows x 2 columns so all six are visible at once.
        for i in range(MAX_TIMES):
            var = tk.StringVar(value=BLANK)
            self.time_vars.append(var)
            var.trace_add("write", lambda *_: self._refresh_preview())
            cell = ttk.Frame(times_box)
            cell.grid(row=i % 3, column=i // 3, sticky="w", padx=(0, 12), pady=2)
            ttk.Label(cell, text=f"Run {i + 1}").pack(side="left", padx=(0, 4))
            ttk.Combobox(cell, textvariable=var, state="readonly", width=7,
                         values=[BLANK, *HOUR_OPTIONS]).pack(side="left")

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
        # Refresh the preview whenever the times, frequency, OR weekday change.
        self.freq_var.trace_add("write", lambda *_: self._refresh_preview())
        self.weekday_var.trace_add("write", lambda *_: self._refresh_preview())
        self.set_times(["10:00", "19:00"])  # sensible default (also paints preview)

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
        ttk.Label(pbox, text="Until:").pack(side="left")
        ttk.Label(pbox, textvariable=self.pause_date_var, width=12,
                  style="Muted.TLabel").pack(side="left", padx=(6, 6))
        ttk.Button(pbox, text="Pick date…", command=self._open_calendar).pack(side="left")
        ttk.Label(pbox, text="time").pack(side="left", padx=(12, 0))
        ttk.Combobox(pbox, textvariable=self.pause_time_var, state="readonly", width=7,
                     values=[BLANK, *HOUR_OPTIONS]).pack(side="left", padx=(6, 12))
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
        for i, var in enumerate(self.time_vars):
            var.set(times[i] if i < len(times) else BLANK)
        self._refresh_preview()

    def set_pause_inputs(self, date: str, time: str = "") -> None:
        self.pause_date_var.set(date)
        self.pause_time_var.set(time or BLANK)

    def _open_calendar(self) -> None:
        try:
            initial = date.fromisoformat(self.pause_date_var.get().strip())
        except ValueError:
            initial = None
        DatePicker(self.frame, on_pick=self.pause_date_var.set, initial=initial)

    def _times(self) -> list[str]:
        return [v.get() for v in self.time_vars if v.get() and v.get() != BLANK]

    def _weekday_idx(self) -> int:
        try:
            return WEEKDAYS.index(self.weekday_var.get())
        except ValueError:
            return 1

    def crontab_text(self) -> str:
        return vm_schedule.build_crontab(self._times(), freq=self.freq_var.get(),
                                         weekday=self._weekday_idx())

    def _refresh_preview(self) -> None:
        if not hasattr(self, "preview"):
            return  # a time var changed before the preview widget exists (during build)
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
        pause_date = self.pause_date_var.get().strip()
        if not pause_date:
            self._notify("Pause", "Pick an 'until' date first.")
            return
        t = self._require_configured()
        if not t:
            return
        time = self.pause_time_var.get()
        if time == BLANK:
            time = ""  # the sentinel means "no time" -> date-only pause
        val = vm_schedule.pause_until_value(pause_date, time)
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
