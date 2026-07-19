"""Pure VM schedule/pause/run-label generators (local/vm_schedule.py).

No gcloud, no Tk — just crontab/pause text and the hour->label mapping the VM
panel and the scraper share.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import vm_schedule as vs  # noqa: E402


def test_run_labels_include_legacy_and_new():
    assert vs.RUN_LABELS[:2] == ("morning", "afternoon")
    assert "evening" in vs.RUN_LABELS and "night" in vs.RUN_LABELS


def test_label_for_hour_buckets():
    assert vs.label_for_hour(10) == "morning"
    assert vs.label_for_hour(13) == "afternoon"
    assert vs.label_for_hour(19) == "evening"
    assert vs.label_for_hour(23) == "night"
    assert vs.label_for_hour(3) == "night"


def test_build_crontab_daily_two_times():
    cron = vs.build_crontab(["10:00", "19:00"])
    # Schedule lines only (drop the managed-block marker comments).
    lines = [ln for ln in cron.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 2
    assert lines[0].startswith("0 10 * * *") and "run_scraper.sh" in lines[0]
    assert lines[1].startswith("0 19 * * *")


def test_build_crontab_wraps_in_managed_markers():
    # Exactly one INPLOYED-SCHEDULE block so a push can strip+replace just it.
    cron = vs.build_crontab(["10:00"])
    assert cron.count(vs.SCHEDULE_BEGIN) == 1
    assert cron.count(vs.SCHEDULE_END) == 1
    assert vs.SCHEDULE_BEGIN in cron.splitlines()[0]


def test_build_crontab_biweekly_uses_epoch_week_parity():
    # Epoch-week parity survives year boundaries; ISO week number (%V) does not.
    cron = vs.build_crontab(["08:30"], freq="biweekly", weekday=1)
    assert r"date +\%s" in cron and "604800" in cron
    assert r"date +\%V" not in cron


def test_build_crontab_weekly_sets_weekday():
    cron = vs.build_crontab(["08:30"], freq="weekly", weekday=1)
    assert "30 8 * * 1" in cron and "run_scraper.sh" in cron


def test_build_crontab_biweekly_guards_command():
    cron = vs.build_crontab(["08:30"], freq="biweekly", weekday=1)
    assert "date" in cron and "run_scraper.sh" in cron  # guarded, not a bare line


def test_validate_rejects_too_many_times():
    errs = vs.validate_schedule(
        ["00:00", "02:00", "04:00", "06:00", "08:00", "10:00", "12:00"])
    assert errs


def test_validate_rejects_times_closer_than_two_hours():
    errs = vs.validate_schedule(["10:00", "11:00"])
    assert errs


def test_validate_rejects_bad_time_format():
    assert vs.validate_schedule(["25:00"])
    assert vs.validate_schedule(["9:5"])


def test_validate_accepts_good_schedule():
    assert vs.validate_schedule(["10:00", "19:00"]) == []


def test_pause_until_value_date_and_datetime():
    assert vs.pause_until_value("2026-07-01") == "2026-07-01"
    assert vs.pause_until_value("2026-07-01", "14:30") == "2026-07-01 14:30"
