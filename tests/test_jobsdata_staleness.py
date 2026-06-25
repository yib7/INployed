"""run_staleness: classify how fresh the latest pipeline run is (Cycle 15 SP4)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402

NOW = datetime(2026, 6, 24, 12, 0, 0)


def test_fresh_within_threshold():
    state, age = jobsdata.run_staleness(NOW - timedelta(hours=4), NOW, 36)
    assert state == "fresh"
    assert 3.9 < age < 4.1


def test_stale_beyond_threshold():
    state, age = jobsdata.run_staleness(NOW - timedelta(hours=50), NOW, 36)
    assert state == "stale"
    assert 49.9 < age < 50.1


def test_boundary_counts_as_fresh():
    state, _ = jobsdata.run_staleness(NOW - timedelta(hours=36), NOW, 36)
    assert state == "fresh"   # exactly at the threshold is still fresh


def test_none_is_stale_with_infinite_age():
    state, age = jobsdata.run_staleness(None, NOW, 36)
    assert state == "stale"
    assert age == float("inf")
