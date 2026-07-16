"""Keep the local LinkedInJobsWatcher scheduled task in step with the VM schedule.

The watcher's Task-Scheduler triggers are registered once by setup_tasks.ps1 and
then never move — so when the VM cron shifts, the local checks fire at the wrong
times. This module regenerates the task from the same local/task.xml template the
installer uses (same {{PLACEHOLDER}} substitution) and re-registers it via
`schtasks /Create /F /TN <name> /XML <file>` — no elevation needed, because the
admin-only <BootTrigger> is stripped (the LogonTrigger plus StartWhenAvailable
already cover boot, the same fallback setup_tasks.ps1 makes).

Everything except `register` is pure (no I/O beyond reading the template), so the
schedule math and XML generation are unit-testable without touching Task Scheduler.
Timezone note: watcher times are the VM's wall-clock run times plus an offset —
correct only while the VM shares the user's timezone (it does; the settings help
text says so).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Overridable for tests / dry runs (a dry run can register a throwaway name
# first, verify, and delete it before touching the real task).
TASK_NAME = "LinkedInJobsWatcher"

# The schedule setup_tasks.ps1 registered and the machine is running today —
# "Restore default local schedule" re-registers exactly this.
DEFAULT_TIMES = ["10:10", "10:20", "10:30", "19:10", "19:20", "19:30"]

DEFAULT_OFFSETS = (30, 50, 70)
TEMPLATE = HERE / "task.xml"

# The contiguous run of daily triggers in the template (verified contiguous in
# task.xml). Greedy `.*` spans first opening tag to last closing tag on purpose.
_CALENDAR_RUN_RE = re.compile(r"<CalendarTrigger>.*</CalendarTrigger>", re.DOTALL)
_BOOT_TRIGGER_RE = re.compile(r"\s*<BootTrigger>.*?</BootTrigger>", re.DOTALL)

_CALENDAR_BLOCK = (
    "<CalendarTrigger>\n"
    "      <StartBoundary>2026-01-01T{time}:00</StartBoundary>\n"
    "      <Enabled>true</Enabled>\n"
    "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
    "    </CalendarTrigger>"
)


def parse_offsets(text, default: tuple[int, ...] = DEFAULT_OFFSETS) -> tuple[int, ...]:
    """Minutes-after-run offsets from a '30,50,70'-style settings string.

    Tolerant of spaces, empties, and junk entries (each is skipped); offsets are
    non-negative ints. Falls back to `default` when nothing valid survives, so a
    mangled settings value can never produce a trigger-less task.
    """
    out: list[int] = []
    for part in str(text or "").split(","):
        part = part.strip()
        try:
            n = int(part)
        except ValueError:
            continue
        if n >= 0:
            out.append(n)
    return tuple(out) if out else tuple(default)


def watcher_times(vm_times, offsets) -> list[str]:
    """Local check times: each VM 'HH:MM' plus each offset, wrapped mod 24h,
    deduplicated and sorted — e.g. ['12:00','20:00'] with (30,50,70) →
    ['12:30','12:50','13:10','20:30','20:50','21:10']."""
    out: set[str] = set()
    for t in vm_times:
        h, m = str(t).strip().split(":")
        base = int(h) * 60 + int(m)
        for off in offsets:
            total = (base + int(off)) % (24 * 60)
            out.add(f"{total // 60:02d}:{total % 60:02d}")
    return sorted(out)


def build_task_xml(times, pythonw: str, script: str, workdir: str, user_id: str,
                   template: Path = TEMPLATE) -> str:
    """Task-Scheduler XML for the watcher with daily triggers at `times`.

    Reads local/task.xml and substitutes its four {{PLACEHOLDER}}s exactly the way
    setup_tasks.ps1 does, then swaps the template's contiguous CalendarTrigger run
    for one generated block per time. The logon/unlock/resume event triggers pass
    through verbatim; the <BootTrigger> is stripped (registering it needs admin).
    """
    xml = Path(template).read_text(encoding="utf-8")
    xml = (xml.replace("{{PYTHONW}}", str(pythonw))
              .replace("{{SCRIPT}}", str(script))
              .replace("{{WORKDIR}}", str(workdir))
              .replace("{{USERID}}", str(user_id)))
    triggers = "\n    ".join(_CALENDAR_BLOCK.format(time=str(t).strip()) for t in times)
    # Replacement via lambda so backslashes in paths can never be misread as
    # regex group references.
    xml = _CALENDAR_RUN_RE.sub(lambda _m: triggers, xml, count=1)
    return _BOOT_TRIGGER_RE.sub("", xml)


def register(times, task_name: str = TASK_NAME, runner=None) -> tuple[bool, str]:
    """(Re-)register the watcher task with daily triggers at `times`.

    Resolves pythonw.exe beside sys.executable (falls back to python.exe — same
    order as setup_tasks.ps1), writes the generated XML UTF-16 to a temp file
    (schtasks' expected task-XML encoding), and runs
    `schtasks /Create /F /TN <task_name> /XML <file>` — /F overwrites in place, so
    no separate delete step. Returns (ok, human-readable message); the temp file
    is always removed. `runner` is injectable so tests never touch Task Scheduler.
    """
    if not list(times):
        # A trigger-less task would silently never run on schedule; refuse rather
        # than register a broken one. (Both callers already guard, but register is
        # public.)
        return False, "Refusing to register a task with no run times."
    runner = runner or subprocess.run
    exe_dir = Path(sys.executable).resolve().parent
    pythonw = exe_dir / "pythonw.exe"
    if not pythonw.exists():
        pythonw = exe_dir / "python.exe"   # flashes a console but still works
    domain, user = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    user_id = f"{domain}\\{user}" if domain else user

    xml = build_task_xml(times, pythonw=str(pythonw), script=str(HERE / "watcher.py"),
                         workdir=str(HERE), user_id=user_id)
    fd, tmp = tempfile.mkstemp(prefix="watcher_task_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:   # BOM included
            fh.write(xml)
        argv = ["schtasks", "/Create", "/F", "/TN", str(task_name), "/XML", tmp]
        try:
            res = runner(argv, capture_output=True, text=True)
        except OSError as exc:
            return False, f"schtasks failed to launch: {exc}"
        out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        ok = getattr(res, "returncode", 1) == 0
        return ok, out or ("Task registered." if ok else "schtasks failed.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
