"""Settings snapshot archive: snapshot / list / load / secrets / prune / delete."""
from datetime import datetime

import envfile
import settings
import settings_archive


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "apply": tmp_path / "apply_config.json",
        "env": tmp_path / ".env",
    }


def _seed(targets, min_score=4):
    settings.save({"min_score": min_score}, targets)        # writes config.json
    envfile.update(targets["env"], {"GEMINI_API_KEYS": "secret-123",
                                    "RESUME_TAILOR_CANDIDATE": "Jane"})  # writes .env


def test_archive_dir_sits_beside_config(tmp_path):
    targets = _targets(tmp_path)
    assert settings_archive.archive_dir(targets) == tmp_path / "settings_archive"


def test_snapshot_copies_existing_files_only(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    snap = settings_archive.snapshot(targets)
    assert snap is not None and snap.is_dir()
    names = {p.name for p in snap.iterdir()}
    assert "config.json" in names and ".env" in names
    assert "search_config.json" not in names  # never created -> not copied


def test_snapshot_none_when_nothing_to_capture(tmp_path):
    assert settings_archive.snapshot(_targets(tmp_path)) is None


def test_snapshot_unique_dir_on_same_second(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    when = datetime(2026, 6, 23, 10, 0, 0)
    a = settings_archive.snapshot(targets, when=when)
    b = settings_archive.snapshot(targets, when=when)
    assert a != b and a.is_dir() and b.is_dir()


def test_list_snapshots_newest_first(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 9, 0, 0))
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 11, 0, 0))
    snaps = settings_archive.list_snapshots(targets)
    assert [s.timestamp.hour for s in snaps] == [11, 9]


def test_load_snapshot_reads_archived_values_not_live(tmp_path):
    targets = _targets(tmp_path)
    settings.save({"min_score": 5}, targets)
    snap = settings_archive.snapshot(targets)
    settings.save({"min_score": 2}, targets)            # live moves on
    assert settings_archive.load_snapshot(snap, targets)["min_score"] == 5


def test_snapshot_secrets_returns_only_secret_fields(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    snap = settings_archive.snapshot(targets)
    secrets = settings_archive.snapshot_secrets(snap, targets)
    assert secrets.get("GEMINI_API_KEYS") == "secret-123"
    assert "RESUME_TAILOR_CANDIDATE" not in secrets       # not a secret field


def test_prune_count_keeps_newest_n(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    for sec in range(5):
        settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 10, 0, sec))
    deleted = settings_archive.prune(settings_archive.PRUNE_COUNT, keep=2, targets=targets)
    remaining = settings_archive.list_snapshots(targets)
    assert len(deleted) == 3 and len(remaining) == 2
    assert {s.timestamp.second for s in remaining} == {4, 3}  # the two newest survive


def test_prune_age_deletes_older_than_days(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    settings_archive.snapshot(targets, when=datetime(2026, 6, 1, 0, 0, 0))   # old
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 0, 0, 0))  # recent
    deleted = settings_archive.prune(settings_archive.PRUNE_AGE, days=7,
                                     targets=targets, now=datetime(2026, 6, 23, 12, 0, 0))
    remaining = settings_archive.list_snapshots(targets)
    assert len(deleted) == 1 and len(remaining) == 1
    assert remaining[0].timestamp.day == 23


def test_prune_off_is_a_noop(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    settings_archive.snapshot(targets, when=datetime(2026, 6, 23, 10, 0, 0))
    deleted = settings_archive.prune(settings_archive.PRUNE_OFF, targets=targets)
    assert deleted == [] and len(settings_archive.list_snapshots(targets)) == 1


def test_delete_snapshot_removes_folder(tmp_path):
    targets = _targets(tmp_path)
    _seed(targets)
    snap = settings_archive.snapshot(targets)
    settings_archive.delete_snapshot(snap)
    assert not snap.exists()
    assert settings_archive.list_snapshots(targets) == []
