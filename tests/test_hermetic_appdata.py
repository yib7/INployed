"""Guard: the suite must never open the user's real %LOCALAPPDATA%\\linkedin_watcher\\.

seen_db.SeenRegistry() (the application tracker + generated-résumé index),
watcher.py (LOG_PATH/STATE_PATH, bound at *import* time) and the apply-queue /
ats-accounts stores all derive their on-disk location from LOCALAPPDATA. A test
that constructs any of them without an explicit tmp path would otherwise read AND
write the user's live files.

That actually happened: pytest runs (some from worktrees) opened the real seen.db
concurrently with the running dashboard + scheduled watcher and corrupted it twice
(2026-06-28, 2026-07-07). `app_status` — the one table with no self-heal — was
wiped both times. conftest now redirects LOCALAPPDATA to a throwaway dir for the
whole session; these tests fail loudly if that redirect is ever removed, instead
of silently corrupting real data again.
"""
import os

_MARK = "inployed-test-appdata"


def test_localappdata_is_redirected_away_from_real_profile():
    la = os.environ.get("LOCALAPPDATA", "")
    assert _MARK in la.lower(), (
        f"LOCALAPPDATA is not the test sandbox (got {la!r}) — tests would touch the "
        "user's real application tracker. See conftest's LOCALAPPDATA redirect."
    )


def test_seen_registry_default_path_is_sandboxed():
    from seen_db import SeenRegistry
    r = SeenRegistry()
    try:
        assert _MARK in str(r.path).lower(), str(r.path)
    finally:
        r.close()


def test_watcher_log_path_is_sandboxed():
    import watcher
    assert _MARK in str(watcher.LOG_PATH).lower(), str(watcher.LOG_PATH)
