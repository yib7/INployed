"""Regression: a job marked seen must STAY seen across a fresh master.

The local SQLite registry is the source of truth for seen-state; the is_seen
column inside each synced .csv.gz is only a projection. When the VM ships a new
master with every is_seen="no", the dashboard must re-overlay the registry on
load (reload_data -> reconcile_is_seen) so a job the user already triaged does
NOT resurface in the High Score (Unseen) tab. These guard that overlay, and the
invariant that it can only promote no->yes (never un-see a row).

Run:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from csv_io import reconcile_is_seen  # noqa: E402
from seen_db import SeenRegistry  # noqa: E402


def _fresh_master(seen_default="no"):
    """Mimics a VM-shipped master where the is_seen column was reset."""
    return pd.DataFrame([
        {"job_posting_id": 1, "score": 5, "is_seen": seen_default},
        {"job_posting_id": 2, "score": 4, "is_seen": seen_default},
        {"job_posting_id": 3, "score": 5, "is_seen": seen_default},
    ])


def test_marked_seen_survives_fresh_master(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.mark(["1", "3"])  # user triaged jobs 1 and 3 in a previous session

    df, n = reconcile_is_seen(_fresh_master(), reg)

    assert n == 2, "both registry-seen rows should have been promoted"
    seen = dict(zip(df["job_posting_id"].astype(str), df["is_seen"]))
    assert seen == {"1": "yes", "2": "no", "3": "yes"}
    reg.close()


def test_empty_registry_never_unsees(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")  # nothing marked
    df = _fresh_master()
    df.loc[df["job_posting_id"] == 2, "is_seen"] = "yes"  # already seen on disk

    out, n = reconcile_is_seen(df, reg)

    assert n == 0  # an empty registry must not touch any row
    assert dict(zip(out["job_posting_id"].astype(str), out["is_seen"]))["2"] == "yes"
    reg.close()


def test_reconcile_only_promotes(tmp_path):
    """A row already seen on disk but absent from the registry stays seen."""
    reg = SeenRegistry(tmp_path / "seen.db")
    reg.mark(["1"])
    df = _fresh_master()
    df.loc[df["job_posting_id"] == 2, "is_seen"] = "yes"  # seen on disk only

    out, _ = reconcile_is_seen(df, reg)

    seen = dict(zip(out["job_posting_id"].astype(str), out["is_seen"]))
    assert seen == {"1": "yes", "2": "yes", "3": "no"}
    reg.close()


if __name__ == "__main__":
    import tempfile

    for fn in (test_marked_seen_survives_fresh_master,
               test_empty_registry_never_unsees, test_reconcile_only_promotes):
        fn(Path(tempfile.mkdtemp()))
    print("SEEN RECONCILE TESTS OK")
