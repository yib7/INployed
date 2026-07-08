"""Durable outbox: full master rows from LOCAL scrapes / manual adds, queued for the VM.

A local run's data used to live only on this PC. Now every successful dashboard scrape
and manual add writes its new rows (full local-master rows, JD included) plus the whole
run_stats.csv into <repo>/outbox/, and the dashboard best-effort-pushes every pending
outbox file to the VM's ~/incoming/ (vm_sync transport). merge_incoming.py on the VM
folds them into the real master before each scheduled scrape. Files are deleted locally
only after a confirmed push (scp exit 0), so a failed push simply retries on the next
scrape/add; re-pushing an already-merged file is a no-op (the VM merge dedups).

Pure pandas + pathlib: no Qt, no gcloud here (push argv/runner live in vm_sync).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
OUTBOX_DIR = REPO_ROOT / "outbox"
MASTER_CSV = REPO_ROOT / "linkedin_jobs_master.csv"
RUN_STATS_CSV = REPO_ROOT / "run_stats.csv"


def _stamp() -> str:
    # Microseconds keep two same-second writes (e.g. rapid manual adds) distinct.
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def snapshot_run_files(base: Path | None = None) -> dict[Path, float]:
    """{scored-run-file: mtime} for every local run file, taken BEFORE a scrape so
    new_run_ids can tell which files the scrape produced or rewrote (a same-day
    re-run overwrites the same filename, so presence alone is not enough)."""
    import jobsdata

    out: dict[Path, float] = {}
    for p in jobsdata.local_run_files(base):
        try:
            out[p] = p.stat().st_mtime
        except OSError:
            pass
    return out


def new_run_ids(before: dict[Path, float], base: Path | None = None) -> list[str]:
    """job_posting_ids from run files that appeared or changed since `before`."""
    import jobsdata

    ids: list[str] = []
    seen: set[str] = set()
    for p in jobsdata.local_run_files(base):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if p in before and before[p] == mtime:
            continue
        try:
            df = pd.read_csv(p, usecols=lambda c: c == "job_posting_id", dtype=str,
                             compression="gzip" if p.suffix == ".gz" else "infer")
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        if "job_posting_id" not in df.columns:
            continue
        for jid in df["job_posting_id"].dropna().astype(str):
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids


def write_rows_outbox(ids, master_csv: Path | None = None,
                      outbox_dir: Path | None = None) -> Path | None:
    """Write the full local-master rows for `ids` (JD + score columns included) to
    outbox/local_rows_<stamp>.csv.gz. None when there is nothing to write — no ids,
    master missing/unreadable, or no id matched (best-effort by design: the outbox
    must never fail a scrape)."""
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        return None
    master = Path(master_csv) if master_csv is not None else MASTER_CSV
    if not master.exists():
        return None
    try:
        df = pd.read_csv(master, dtype={"job_posting_id": str})
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    if "job_posting_id" not in df.columns:
        return None
    id_set = set(ids)
    have = set(df["job_posting_id"].astype(str))
    not_found = id_set - have
    if not_found:
        # A local job that never landed in the master won't reach the VM — surface
        # it so a lost row is visible instead of silently dropped.
        log.warning("write_rows_outbox: %d of %d id(s) not in master, not queued: %s",
                    len(not_found), len(id_set),
                    ", ".join(sorted(not_found)[:20]))
    rows = df[df["job_posting_id"].astype(str).isin(id_set)]
    if rows.empty:
        return None
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    ob.mkdir(parents=True, exist_ok=True)
    path = ob / f"local_rows_{_stamp()}.csv.gz"
    rows.to_csv(path, index=False, compression="gzip")
    return path


def write_stats_outbox(stats_csv: Path | None = None,
                       outbox_dir: Path | None = None) -> Path | None:
    """Queue the WHOLE local run_stats.csv (idempotent + monotonic, like
    write_external_exclude_ids — the VM-side merge dedups, so no cursor state)."""
    stats = Path(stats_csv) if stats_csv is not None else RUN_STATS_CSV
    if not stats.exists():
        return None
    try:
        data = stats.read_bytes()
    except OSError:
        return None
    if not data.strip():
        return None
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    ob.mkdir(parents=True, exist_ok=True)
    path = ob / f"local_stats_{_stamp()}.csv"
    path.write_bytes(data)
    return path


def pending_files(outbox_dir: Path | None = None) -> list[Path]:
    """Every queued outbox file, name-sorted (stamps make that chronological)."""
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    if not ob.is_dir():
        return []
    return sorted([*ob.glob("local_rows_*.csv.gz"), *ob.glob("local_stats_*.csv")])


def prune_stats(outbox_dir: Path | None = None) -> int:
    """Drop all but the newest queued local_stats file. Each one is a FULL copy
    of run_stats.csv (monotonic — the VM-side merge dedups), so the newest
    strictly supersedes the rest and pushing them all is pure waste. Returns
    how many were removed."""
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    if not ob.is_dir():
        return 0
    removed = 0
    for p in sorted(ob.glob("local_stats_*.csv"))[:-1]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# --- pushed-ids memory --------------------------------------------------------
# Rows scp'd to the VM's ~/incoming/ are invisible until the NEXT scheduled run
# merges and re-uploads the Drive master. Without a memory of what is in flight,
# every unsynced-rows sweep in that window would re-queue (and re-push) the same
# rows. pushed_ids.json remembers them; ids are forgotten once they show up in
# the Drive master (the round trip completed). Re-pushing after a lost state
# file is harmless — the VM merge dedups with the existing row winning.

def _pushed_ids_path(outbox_dir: Path | None = None) -> Path:
    ob = Path(outbox_dir) if outbox_dir is not None else OUTBOX_DIR
    return ob / "pushed_ids.json"


def load_pushed_ids(outbox_dir: Path | None = None) -> set[str]:
    """Ids pushed to the VM but not yet seen back in the Drive master. Empty on
    a missing/corrupt state file (fail-open: worst case is a redundant push)."""
    p = _pushed_ids_path(outbox_dir)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def _save_pushed_ids(ids: set[str], outbox_dir: Path | None = None) -> None:
    p = _pushed_ids_path(outbox_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except OSError:
        pass  # state is an optimization only; never fail a push over it


def record_pushed_ids(ids, outbox_dir: Path | None = None) -> None:
    """Union `ids` into the in-flight set."""
    new = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if not new:
        return
    _save_pushed_ids(load_pushed_ids(outbox_dir) | new, outbox_dir)


def prune_pushed_ids(drive_ids, outbox_dir: Path | None = None) -> None:
    """Forget in-flight ids that have round-tripped into the Drive master."""
    cur = load_pushed_ids(outbox_dir)
    kept = cur - {str(i) for i in drive_ids}
    if kept != cur:
        _save_pushed_ids(kept, outbox_dir)


def _ids_from_csv(path: Path) -> set[str] | None:
    """job_posting_id set from a .csv/.csv.gz, or None when the file is missing
    or unreadable (callers decide whether None fails open or closed)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        comp = "gzip" if path.suffix == ".gz" else "infer"
        df = pd.read_csv(path, usecols=lambda c: c == "job_posting_id", dtype=str,
                         compression=comp)
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    if "job_posting_id" not in df.columns:
        return None
    return set(df["job_posting_id"].dropna().astype(str))


def unsynced_master_ids(drive_master, master_csv: Path | None = None,
                        outbox_dir: Path | None = None) -> list[str]:
    """The catch-all sweep: ids in the LOCAL master that the Drive master lacks
    and that are neither queued in a pending rows file nor already in flight to
    the VM. These are rows that would otherwise be stranded on this PC forever —
    e.g. a snapshot recovered outside the dashboard, or a push that never got a
    retry. Returns [] when either master is missing/unreadable: failing CLOSED
    here matters, because an unreadable Drive master must not make the sweep
    queue (and push) the entire local master."""
    if drive_master is None:
        return []
    local_ids = _ids_from_csv(Path(master_csv) if master_csv is not None else MASTER_CSV)
    drive_ids = _ids_from_csv(Path(drive_master))
    if local_ids is None or drive_ids is None:
        return []
    prune_pushed_ids(drive_ids, outbox_dir)
    skip = drive_ids | load_pushed_ids(outbox_dir)
    for f in pending_files(outbox_dir):
        if f.name.startswith("local_rows_"):
            queued = _ids_from_csv(f)
            if queued:
                skip |= queued
    return sorted(local_ids - skip)


def push_outbox(target, outbox_dir: Path | None = None, log=None,
                runner=None) -> tuple[int, int]:
    """Best-effort scp of every pending outbox file to the VM's ~/incoming/.

    A file is deleted locally ONLY when its scp exits 0; anything else (unconfigured
    VM, nonzero exit, runner exception) leaves it queued for the next scrape/manual
    add. Returns (pushed, kept). Never raises — the caller is mid-scrape/add and a
    sync failure must not surface as a scrape failure.
    """
    import vm_sync

    def note(msg: str) -> None:
        if log is not None:
            try:
                log.write(msg + "\n")
                log.flush()
            except Exception:  # noqa: BLE001 - logging must not break the push
                pass

    run = runner if runner is not None else vm_sync.run_cmd
    dropped = prune_stats(outbox_dir)
    if dropped:
        note(f"outbox push: pruned {dropped} superseded stats file(s)")
    files = pending_files(outbox_dir)
    if not files:
        return (0, 0)
    if target is None or not target.configured():
        note(f"outbox push: no VM configured — keeping {len(files)} queued file(s)")
        return (0, len(files))
    pushed = kept = 0
    for f in files:
        try:
            res = run(target.push_outbox_file_cmd(str(f)))
            rc = getattr(res, "returncode", 1)
        except Exception as e:  # noqa: BLE001 - keep the file, try the rest
            note(f"outbox push: {f.name} ERROR ({e}) — kept for retry")
            kept += 1
            continue
        if rc == 0:
            if f.name.startswith("local_rows_"):
                # Remember what is now in flight so the unsynced-rows sweep
                # doesn't re-queue these before the VM's next merge round-trips
                # them into the Drive master.
                record_pushed_ids(_ids_from_csv(f) or (), outbox_dir)
            try:
                f.unlink()
            except OSError:
                pass
            pushed += 1
            note(f"outbox push: {f.name} OK")
        else:
            kept += 1
            err = (getattr(res, "stderr", "") or getattr(res, "stdout", "")).strip()
            note(f"outbox push: {f.name} FAILED (exit {rc}) {err} — kept for retry")
    return (pushed, kept)


def sync_back(target, drive_master, master_csv: Path | None = None,
              outbox_dir: Path | None = None, log=None,
              runner=None) -> tuple[int, int, int]:
    """One-call local→VM data reconcile: queue any local-master rows the Drive
    master lacks (see unsynced_master_ids), then push every pending outbox file.
    Returns (queued_ids, pushed_files, kept_files). Never raises — callers are
    background hooks (the watcher, post-scrape) where a sync problem must never
    become a crash."""
    try:
        ids = unsynced_master_ids(drive_master, master_csv=master_csv,
                                  outbox_dir=outbox_dir)
        if ids:
            write_rows_outbox(ids, master_csv=master_csv, outbox_dir=outbox_dir)
        pushed, kept = push_outbox(target, outbox_dir=outbox_dir, log=log,
                                   runner=runner)
        return (len(ids), pushed, kept)
    except Exception as e:  # noqa: BLE001 - best-effort by contract
        if log is not None:
            try:
                log.write(f"sync_back: error ({e}) — skipped\n")
                log.flush()
            except Exception:  # noqa: BLE001
                pass
        return (0, 0, 0)
