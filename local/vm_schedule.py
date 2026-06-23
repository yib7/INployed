"""Pure helpers for the VM scraper schedule, pause-until, and run labels.

No Tkinter, no gcloud — just artifact generators the dashboard's VM panel and
tests use. Run labels are re-exported from the repo-root `run_labels` module (the
one scraper.py / score_jobs.py import on the VM), so there is a single source of
truth for which hour maps to which label.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# run_labels.py lives at the repo root (so the VM can import it standalone).
# Make it importable when this module is loaded from local/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from run_labels import RUN_LABELS, label_for_hour  # noqa: E402,F401  re-exported

FREQS = ("daily", "weekly", "biweekly")
MAX_TIMES_PER_DAY = 6
MIN_GAP_MINUTES = 120
DEFAULT_CMD = "~/run_scraper.sh"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _minutes(t: str) -> int:
    h, m = str(t).strip().split(":")
    return int(h) * 60 + int(m)


def validate_schedule(times, freq: str = "daily") -> list[str]:
    """Human-readable problems with a schedule; [] means valid. Enforces valid
    HH:MM, at most 6 times/day, and at least a 2-hour gap between run times."""
    errs: list[str] = []
    if freq not in FREQS:
        errs.append(f"Unknown frequency {freq!r} (use one of {', '.join(FREQS)}).")
    if not times:
        errs.append("Add at least one run time.")
        return errs
    bad = [t for t in times if not _TIME_RE.match(str(t).strip())]
    if bad:
        errs.append("Times must be 24-hour HH:MM (e.g. 09:30, 19:00): " + ", ".join(bad))
        return errs  # unparseable -> can't range-check the rest
    if len(times) > MAX_TIMES_PER_DAY:
        errs.append(f"At most {MAX_TIMES_PER_DAY} run times per day (got {len(times)}).")
    mins = sorted(_minutes(t) for t in times)
    for a, b in zip(mins, mins[1:]):
        if b - a < MIN_GAP_MINUTES:
            errs.append(f"Run times must be at least {MIN_GAP_MINUTES // 60} hours apart.")
            break
    return errs


def build_crontab(times, cmd: str = DEFAULT_CMD, freq: str = "daily",
                  weekday: int = 0) -> str:
    """Render crontab lines for the given run times.

    weekday: 0=Sun .. 6=Sat (cron convention), used by weekly/biweekly. Biweekly
    guards the command so it fires only on even ISO week numbers (every other
    week) — '%' is escaped as '\\%' because cron treats it specially.
    """
    dow = "*" if freq == "daily" else str(weekday)
    lines: list[str] = []
    for t in times:
        h, m = (int(x) for x in str(t).strip().split(":"))
        when = f"{m} {h} * * {dow}"
        if freq == "biweekly":
            lines.append(rf"{when} [ $(( $(date +\%V) \% 2 )) -eq 0 ] && {cmd}")
        else:
            lines.append(f"{when} {cmd}")
    return "\n".join(lines)


def pause_until_value(date: str, time: str | None = None) -> str:
    """Content for the VM's ~/pause_until file. Date-only ('YYYY-MM-DD') or
    date+time ('YYYY-MM-DD HH:MM'); run_scraper.sh compares it lexically."""
    date = str(date).strip()
    if time and str(time).strip():
        return f"{date} {str(time).strip()}"
    return date
