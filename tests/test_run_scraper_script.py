"""Pin the VM cron wiring in scripts/run_scraper.sh so the retention prune
can't silently drop out of the pipeline.

The retention prune (prune_master.py) must run AFTER the scoring pass (so this
run's rescore still sees today's full descriptions) and BEFORE the master CSV
is gzipped and uploaded to Drive (so the uploaded master is the pruned one).
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_scraper.sh"


def test_prune_master_is_wired_between_score_and_master_upload():
    text = SCRIPT.read_text(encoding="utf-8")

    i_prune = text.find("prune_master.py")
    i_score = text.find("score_jobs.py")
    # The master upload: gzip the master then rclone copyto the master gz.
    i_gzip = text.find("gzip -c ~/linkedin_jobs_master.csv")
    i_master_copy = text.find("rclone copyto /tmp/linkedin_jobs_master.csv.gz")

    assert i_prune != -1, "prune_master.py is not invoked by run_scraper.sh"
    assert i_score != -1, "score_jobs.py step missing (test anchor stale?)"
    assert i_gzip != -1, "master gzip step missing (test anchor stale?)"
    assert i_master_copy != -1, "master rclone upload missing (test anchor stale?)"

    assert i_score < i_prune, "prune must run AFTER score_jobs.py (rescore needs today's descriptions)"
    assert i_prune < i_gzip, "prune must run BEFORE the master is gzipped"
    assert i_prune < i_master_copy, "prune must run BEFORE the master is uploaded"


def test_prune_invocation_is_best_effort():
    """set -e is active; a nonzero prune exit must NOT abort the cron run."""
    text = SCRIPT.read_text(encoding="utf-8")
    prune_line = next(
        (ln for ln in text.splitlines() if "prune_master.py" in ln and ln.strip().startswith("python")),
        None,
    )
    assert prune_line is not None, "no `python ... prune_master.py` invocation line found"
    assert "||" in prune_line, "prune invocation must be best-effort (guarded with `|| ...`) under set -e"
