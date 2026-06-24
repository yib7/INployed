"""Settings snapshots: a dated copy of every settings file, browse + restore.

Every successful Save writes a snapshot folder
(``settings_archive/<YYYY-MM-DD_HH-MM-SS>/``) holding a copy of each settings
file that exists — ``config.json``, ``search_config.json``,
``scoring_config.json``, ``apply_config.json`` and ``.env``. The user chose
self-contained snapshots, so the copy of ``.env`` carries the SAME secrets the
live file does: the archive directory is therefore git-ignored, and secret
values are never logged or surfaced in the UI — they only ride along inside the
copied ``.env`` so a restore can put them back.

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
_SNAPSHOT_TARGETS = ("config", "search", "scoring", "apply", "env")

# Prune policy names — also the choice values of the archive_prune_mode setting.
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


def prune(mode: str, *, keep: int = 20, days: int = 30,
          targets: dict | None = None, now: datetime | None = None) -> list[Path]:
    """Apply a retention policy; return the snapshot paths deleted.

    ``PRUNE_OFF`` (or any unknown mode) deletes nothing. ``PRUNE_COUNT`` keeps the
    newest ``keep`` and deletes the rest; ``PRUNE_AGE`` deletes snapshots older
    than ``days`` days.
    """
    snaps = list_snapshots(targets)  # newest first
    now = now or datetime.now()
    if mode == PRUNE_COUNT:
        doomed = snaps[max(keep, 0):]
    elif mode == PRUNE_AGE:
        cutoff = now - timedelta(days=days)
        doomed = [s for s in snaps if s.timestamp < cutoff]
    else:
        doomed = []
    for s in doomed:
        delete_snapshot(s.path)
    return [s.path for s in doomed]
