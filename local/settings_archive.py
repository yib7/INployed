"""Settings snapshots: a dated copy of every settings file, browse + restore.

Every successful Save writes a snapshot folder
(``settings_archive/<YYYY-MM-DD_HH-MM-SS>/``) holding a copy of each settings
file that exists — ``config.json``, ``search_config.json``, ``scoring_config.json``
and ``.env``. The user chose self-contained snapshots, so the copy of ``.env``
carries the SAME secrets the live file does: the archive directory is therefore
git-ignored, and secret values are never logged or surfaced in the UI — they only
ride along inside the copied ``.env`` so a restore can put them back.

Restore reads a snapshot back the same way ``settings.load`` reads the live
files: point a ``targets`` mapping at the snapshot folder. The dashboard loads
those values into the Settings form for review and applies them on the next Save.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import envfile
import settings

ARCHIVE_DIRNAME = "settings_archive"
TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
# The settings files a snapshot copies, by target id (the ids settings.py uses).
# No "apply": settings.py dropped that vestigial target, so a legacy repo-root
# apply_config.json is no longer snapshotted — matching apply_answers.json, the
# live answer store, which never was.
_SNAPSHOT_TARGETS = ("config", "search", "scoring", "env")

# Prune policy names. PRUNE_OFF is still a live value — it is the same string as
# settings.ARCHIVE_KEEP_ALL, the default of the archive_mode setting that replaced
# the old archive_prune_* keys. PRUNE_COUNT / PRUNE_AGE are now LEGACY and their
# prune() arms are unreachable from the app: no setting writes those strings, and
# settings._legacy_archive_mode TRANSLATES a stored one into the new vocabulary
# rather than passing it through. Both are retained for direct callers and to keep
# prune()'s contract stable across the merge; the schema key that still names
# PRUNE_COUNT is settings._LEGACY_PRUNE_COUNT, pinned equal to it by
# test_the_migration_recognises_the_pruners_legacy_count_mode.
PRUNE_OFF = "Keep everything"
PRUNE_COUNT = "Keep newest N"
PRUNE_AGE = "Delete older than N days"


def archive_dir(targets: dict | None = None) -> Path:
    """Where snapshots live: a ``settings_archive/`` folder beside config.json, so
    a test that points ``targets`` at a tmp dir archives into that same tmp dir."""
    targets = settings._resolve_targets(targets)
    config_path = targets.get("config")
    parent = Path(config_path).parent if config_path else settings.HERE
    return parent / ARCHIVE_DIRNAME


@dataclass(frozen=True)
class Snapshot:
    """One saved snapshot: its folder and the time it was taken."""

    path: Path
    timestamp: datetime

    @property
    def label(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:19], TS_FORMAT)
    except ValueError:
        return None


def _unique_dir(base: Path, stamp: str) -> Path:
    """A non-existing folder under ``base`` for ``stamp`` (suffix -2, -3, ... on a
    same-second collision)."""
    cand = base / stamp
    n = 2
    while cand.exists():
        cand = base / f"{stamp}_{n}"
        n += 1
    return cand


def snapshot(targets: dict | None = None, when: datetime | None = None) -> Path | None:
    """Copy every existing settings file into a new dated folder; return that folder
    (or ``None`` if no settings file exists yet, so there is nothing to snapshot)."""
    targets = settings._resolve_targets(targets)
    when = when or datetime.now()
    files = []
    for tid in _SNAPSHOT_TARGETS:
        p = targets.get(tid)
        if p is not None and Path(p).is_file():
            files.append(Path(p))
    if not files:
        return None
    dest = _unique_dir(archive_dir(targets), when.strftime(TS_FORMAT))
    dest.mkdir(parents=True, exist_ok=True)
    for src in files:
        shutil.copy2(src, dest / src.name)
    return dest


def snapshot_targets(snap_path: Path, targets: dict | None = None) -> dict:
    """A settings ``targets`` mapping pointing at the files inside a snapshot folder.
    A file the snapshot is missing simply maps to a non-existing path (so
    ``settings.load`` falls back to that field's default)."""
    targets = settings._resolve_targets(targets)
    snap_path = Path(snap_path)
    out: dict = {}
    for tid in _SNAPSHOT_TARGETS:
        live = targets.get(tid)
        if live is not None:
            out[tid] = snap_path / Path(live).name
    return out


def load_snapshot(snap_path: Path, targets: dict | None = None) -> dict:
    """The snapshot's values in ``settings.load()`` shape (schema key -> value/default)."""
    return settings.load(snapshot_targets(snap_path, targets))


def snapshot_secrets(snap_path: Path, targets: dict | None = None) -> dict:
    """The snapshot's secret env values, for staging into a restore.

    Write-only: these are never displayed — the caller holds them until the next
    Save. Only secrets actually present (non-blank) in the snapshot's ``.env`` are
    returned, so restoring an old snapshot never silently clears a newer key.
    """
    stargets = snapshot_targets(snap_path, targets)
    env_path = stargets.get("env")
    if env_path is None or not Path(env_path).is_file():
        return {}
    stored = envfile.read(Path(env_path))
    out: dict = {}
    for f in settings.SETTINGS_SCHEMA:
        if f.secret and str(stored.get(f.key, "")).strip():
            out[f.key] = str(stored[f.key])
    return out


def list_snapshots(targets: dict | None = None) -> list[Snapshot]:
    """All snapshots, newest first."""
    base = archive_dir(targets)
    if not base.is_dir():
        return []
    snaps: list[Snapshot] = []
    for child in base.iterdir():
        if child.is_dir():
            ts = _parse_ts(child.name)
            if ts is not None:
                snaps.append(Snapshot(child, ts))
    snaps.sort(key=lambda s: s.timestamp, reverse=True)
    return snaps


def delete_snapshot(snap_path: Path) -> None:
    snap_path = Path(snap_path)
    if snap_path.is_dir():
        shutil.rmtree(snap_path)


def _keep_from_mode(mode: str) -> int | None:
    """The N in an ``archive_mode`` like "Keep newest 20", else ``None``.

    Every LEGACY mode string is None-safe by construction — their counts live in
    separate config keys, so ``PRUNE_COUNT`` ends in the literal letter N and
    ``PRUNE_AGE`` in "days". That is what lets the counted arm be tried FIRST in
    ``prune()`` without shadowing either of them.

    ``isdecimal``, not ``isdigit``: the latter is True for characters ``int()``
    rejects (superscripts), which would raise out of a Qt save slot that only
    catches ``OSError``.
    """
    tail = str(mode).rsplit(" ", 1)[-1]
    return int(tail) if tail.isdecimal() else None


def prune(mode: str, *, keep: int = 20, days: int = 30,
          targets: dict | None = None, now: datetime | None = None) -> list[Path]:
    """Apply a retention policy; return the snapshot paths deleted.

    ``PRUNE_OFF`` (or any unknown mode) deletes nothing. A mode carrying its own
    count — the "Keep newest 20" / "Keep newest 100" shape of the ``archive_mode``
    setting — keeps that many and deletes the rest, ignoring ``keep``.

    The ``keep`` / ``days`` keywords serve the two LEGACY modes: ``PRUNE_COUNT``
    keeps the newest ``keep``; ``PRUNE_AGE`` deletes snapshots older than ``days``
    days. Keeping this signature is deliberate — it is what let the merge of the
    four archive_* settings into one ``archive_mode`` leave every caller and every
    test of this module untouched.
    """
    snaps = list_snapshots(targets)  # newest first
    now = now or datetime.now()
    # Truthiness, not `is not None`: a zero count falls through to "delete
    # nothing" rather than emptying the archive — including the snapshot the same
    # Save just wrote. No schema choice can produce a zero; testing it this way
    # makes that a structural property of the PRIMARY arm rather than an accident.
    keep_n = _keep_from_mode(mode)
    if keep_n:
        doomed = snaps[keep_n:]
    elif mode == PRUNE_COUNT:
        doomed = snaps[max(keep, 0):]
    elif mode == PRUNE_AGE:
        cutoff = now - timedelta(days=days)
        doomed = [s for s in snaps if s.timestamp < cutoff]
    else:
        doomed = []
    for s in doomed:
        delete_snapshot(s.path)
    return [s.path for s in doomed]
