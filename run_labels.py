"""Run-label buckets shared by scraper.py, score_jobs.py and the dashboard.

A scrape run is labelled by the hour it starts, so the dashboard can show
morning / afternoon / evening / night runs from a configurable schedule.

Kept dependency-free and at the repo root so the VM (which copies scraper.py and
score_jobs.py to the home dir, with no `local/` package) can import it
standalone — exactly like keypool.py.
"""
from __future__ import annotations

# Order matters for display. Legacy data used only "morning"/"evening"; both stay
# in the set so old run folders are still discovered.
RUN_LABELS = ("morning", "afternoon", "evening", "night")


def label_for_hour(hour: int) -> str:
    """Map a 24-hour hour to its run label."""
    h = int(hour) % 24
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"
