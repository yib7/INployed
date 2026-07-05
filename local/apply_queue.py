"""The batch auto-apply queue: an atomic JSON store + the agent CLI (SP2).

Two processes share this queue: the dashboard (SP3) enqueues jobs while the
tailor runs, and the apply agent (SP4) drains it — `claim` one job, fill the
application in Chrome, `finish` it parked at the review page (never submitted).
The store lives beside seen.db in %LOCALAPPDATA%\\linkedin_watcher\\
(apply_queue.json; APPLY_QUEUE_PATH overrides, read at call time so tests stay
hermetic).

Concurrency model: every MUTATION runs inside `locked()` — an exclusive byte-0
lock on the sidecar apply_queue.json.lock (msvcrt on Windows, fcntl elsewhere),
retried every LOCK_RETRY seconds until LOCK_TIMEOUT then QueueLockTimeout —
wrapping a load -> mutate -> atomic_write_json cycle. READS are lock-free on
purpose: atomic_write_json swaps the file in with os.replace, so a reader never
sees a partial file, only the previous or the next complete state. A corrupt /
unparseable queue starts fresh (a RuntimeWarning says so) and is renamed to
apply_queue.json.corrupt-<stamp> by the next LOCKED mutation — lock-free
readers never rename (they could race a writer that already quarantined and
rewrote a healthy file). The queue is re-buildable from the dashboard, never
precious.

Entry lifecycle: tailoring -> queued -> in_progress -> ready_to_submit |
needs_human | failed (the last three are terminal). Every entry always carries
every field, so consumers never .get()-dance around missing keys.

This module NEVER returns, prints, or stores a password and never touches
keyring — credentials are ats_accounts.py's job, and even there only the
clipboard ever carries the secret.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jsonutil import atomic_write_json  # noqa: E402  (needs HERE on sys.path)

# Dashboard config (auto_apply_* keys land here via SP3's Settings UI). Module
# constant so tests can monkeypatch it away from the real local/config.json.
CONFIG_JSON = HERE / "config.json"

STATUSES = ("tailoring", "queued", "in_progress",
            "ready_to_submit", "needs_human", "failed")
TERMINAL = frozenset(("ready_to_submit", "needs_human", "failed"))

ARTIFACT_KEYS = ("folder", "resume_pdf", "cover_letter_pdf", "cover_letter_txt",
                 "apply_md", "application_record")
ATS_KEYS = ("domain", "system", "account_status")
ATS_SYSTEMS = ("linkedin", "workday", "greenhouse", "lever", "icims", "other")

# Sidecar-lock tuning. Module-level (not baked into signatures) so tests can
# monkeypatch LOCK_TIMEOUT down instead of waiting out the real 5 s.
LOCK_TIMEOUT = 5.0    # seconds until QueueLockTimeout
LOCK_RETRY = 0.025    # seconds between lock attempts


class QueueLockTimeout(TimeoutError):
    """Could not take the queue's sidecar lock within LOCK_TIMEOUT."""


class UnknownJobError(KeyError):
    """No queue entry with that job_posting_id."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── paths + locking ──────────────────────────────────────────────────────────

def queue_path(path: Optional[Path] = None) -> Path:
    """The queue file: explicit arg > APPLY_QUEUE_PATH env (read at call time,
    so tests can monkeypatch env) > the shared linkedin_watcher appdata dir."""
    if path is not None:
        return Path(path)
    env = os.environ.get("APPLY_QUEUE_PATH", "").strip()
    if env:
        return Path(env)
    appdata = Path(os.environ.get("LOCALAPPDATA",
                                  str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "apply_queue.json"


def _lock_byte0(fh) -> None:
    """One non-blocking exclusive-lock attempt on byte 0 (raises OSError when
    held elsewhere). msvcrt on Windows, fcntl.flock elsewhere — same split as
    locks.SingleInstance."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)  # msvcrt.locking is positional; always lock byte 0
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte0(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked(path: Optional[Path] = None, timeout: Optional[float] = None):
    """Exclusive cross-process lock on the queue's sidecar .lock file.

    Blocking-with-timeout: retries every LOCK_RETRY seconds and raises
    QueueLockTimeout after `timeout` (default LOCK_TIMEOUT) seconds. Every
    mutation wraps its load -> mutate -> atomic-write cycle in this.
    """
    qp = queue_path(path)
    qp.parent.mkdir(parents=True, exist_ok=True)
    lock_file = qp.with_name(qp.name + ".lock")
    deadline = time.monotonic() + (LOCK_TIMEOUT if timeout is None else timeout)
    fh = open(lock_file, "a+b")
    got = False
    try:
        while True:
            try:
                _lock_byte0(fh)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise QueueLockTimeout(
                        f"could not lock {lock_file} within "
                        f"{LOCK_TIMEOUT if timeout is None else timeout:.1f}s "
                        "(another dashboard/agent process is holding it)") from None
                time.sleep(LOCK_RETRY)
        yield
    finally:
        if got:
            try:
                _unlock_byte0(fh)
            except OSError:
                pass
        fh.close()


# ── load / save ──────────────────────────────────────────────────────────────

def _fresh() -> Dict[str, Any]:
    return {"version": 1, "jobs": []}


def _quarantine(qp: Path) -> None:
    """Rename an unreadable queue aside (apply_queue.json.corrupt-<stamp>) so the
    next write starts fresh without silently destroying evidence."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = qp.with_name(f"{qp.name}.corrupt-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = qp.with_name(f"{qp.name}.corrupt-{stamp}-{n}")
    try:
        os.replace(qp, target)
        moved = f"moved aside to {target.name}"
    except OSError:
        moved = "could not be moved aside"
    warnings.warn(
        f"apply queue {qp} was corrupt/unparseable ({moved}); starting fresh",
        RuntimeWarning, stacklevel=3)


# How often load() retries a failed read before treating the queue as empty.
_READ_TRIES = 3
_READ_RETRY = 0.02        # seconds between attempts


def load(path: Optional[Path] = None, *, quarantine: bool = False) -> Dict[str, Any]:
    """The queue dict ({"version": 1, "jobs": [...]}). Lock-free by design —
    atomic_write_json means a concurrent reader only ever sees a complete file.

    Missing -> fresh. A read OSError (AV scan, sharing violation) is NOT
    corruption: it is retried briefly, then fresh is returned with the file
    left untouched. A file that reads but doesn't parse to the right shape
    returns fresh too, and is renamed aside (.corrupt-<stamp>) only when
    quarantine=True — which callers may pass ONLY while holding locked():
    a lock-free reader renaming could race a lock-holding writer that already
    quarantined and rewrote a healthy queue, moving the VALID file aside."""
    qp = queue_path(path)
    raw = None
    for attempt in range(_READ_TRIES):
        if not qp.exists():
            return _fresh()
        try:
            raw = qp.read_text(encoding="utf-8")
            break
        except OSError:
            if attempt < _READ_TRIES - 1:
                time.sleep(_READ_RETRY)
    if raw is None:
        warnings.warn(
            f"apply queue {qp} could not be read (transient lock/AV?); "
            "treating as empty, file left in place",
            RuntimeWarning, stacklevel=2)
        return _fresh()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data
    if quarantine:
        _quarantine(qp)
    else:
        warnings.warn(
            f"apply queue {qp} is corrupt/unparseable; treating as empty "
            "(left in place — the next locked mutation quarantines it)",
            RuntimeWarning, stacklevel=2)
    return _fresh()


def _save(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    qp = queue_path(path)
    qp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(qp, data)


def _normalize(e: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill missing schema keys on an entry IN PLACE (hand-edited queues:
    a minimal {"job_posting_id", "status"} entry must not KeyError a mutation
    like e["artifacts"][k]). Never invents timestamps; present keys win."""
    base = new_entry(str(e.get("job_posting_id", "")),
                     apply_url=str(e.get("apply_url") or ""))
    base["queued_at"] = ""
    base["updated_at"] = ""
    for k, v in base.items():
        e.setdefault(k, v)
    if not isinstance(e.get("artifacts"), dict):
        e["artifacts"] = {}
    for k in ARTIFACT_KEYS:
        e["artifacts"].setdefault(k, "")
    if not isinstance(e.get("ats"), dict):
        e["ats"] = {}
    for k in ATS_KEYS:
        e["ats"].setdefault(k, base["ats"][k])
    if not isinstance(e.get("missing_answers"), list):
        e["missing_answers"] = []
    return e


def _find(data: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    for e in data["jobs"]:
        if str(e.get("job_posting_id")) == str(job_id):
            return _normalize(e)
    raise UnknownJobError(f"no queue entry with job_posting_id {job_id!r}")


# ── entries ──────────────────────────────────────────────────────────────────

def infer_ats(apply_url: str) -> Dict[str, str]:
    """{domain, system} guessed from the apply URL's netloc. `other` when the
    host matches none of the known ATS families (or there is no URL)."""
    from urllib.parse import urlsplit
    url = str(apply_url or "").strip()
    netloc = urlsplit(url).netloc.lower() if "://" in url else url.split("/")[0].lower()
    system = "other"
    for token, name in (("linkedin", "linkedin"), ("workday", "workday"),
                        ("greenhouse", "greenhouse"), ("lever.co", "lever"),
                        ("icims", "icims")):
        if token in netloc:
            system = name
            break
    return {"domain": netloc, "system": system}


def new_entry(job_posting_id: str, *, company: str = "", title: str = "",
              apply_url: str = "", is_easy_apply: bool = False,
              batch_id: str = "", status: str = "queued") -> Dict[str, Any]:
    """A complete queue entry — every field always present. The dashboard
    enqueues with status="tailoring" while the tailor runs; set_artifacts flips
    it to "queued" once the PDFs exist. The CLI default is "queued"."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
    now = _now()
    ats = infer_ats(apply_url)
    return {
        "job_posting_id": str(job_posting_id),
        "company": str(company or ""),
        "title": str(title or ""),
        "apply_url": str(apply_url or ""),
        "is_easy_apply": bool(is_easy_apply),
        "batch_id": str(batch_id or ""),
        "status": status,
        "attempts": 0,
        "claimed_by": "",
        "notes": "",
        "tab_note": "",
        "missing_answers": [],
        "artifacts": {k: "" for k in ARTIFACT_KEYS},
        "ats": {"domain": ats["domain"], "system": ats["system"],
                "account_status": ""},
        "queued_at": now if status == "queued" else "",
        "started_at": "",
        "finished_at": "",
        "updated_at": now,
    }


# ── mutations (every one: with locked(): load -> mutate -> atomic write) ─────

def enqueue(entry: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """Upsert by job_posting_id. An existing NON-terminal duplicate is returned
    unchanged (the job is already in flight); a terminal duplicate is replaced
    in place by the fresh entry (a re-run of a finished/failed job)."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        jid = str(entry.get("job_posting_id"))
        for i, e in enumerate(data["jobs"]):
            if str(e.get("job_posting_id")) == jid:
                if e.get("status") not in TERMINAL:
                    return dict(e)
                data["jobs"][i] = entry
                _save(data, path)
                return dict(entry)
        data["jobs"].append(entry)
        _save(data, path)
        return dict(entry)


def set_artifacts(job_id: str, artifacts: Dict[str, str],
                  path: Optional[Path] = None) -> Dict[str, Any]:
    """Fill artifact paths (unknown keys ignored). A "tailoring" entry becomes
    "queued" — the tailor is done, the job may now be claimed."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        for k in ARTIFACT_KEYS:
            if k in artifacts:
                e["artifacts"][k] = str(artifacts[k] or "")
        if e["status"] == "tailoring":
            e["status"] = "queued"
            e["queued_at"] = _now()
        e["updated_at"] = _now()
        _save(data, path)
        return dict(e)


def claim(claimed_by: str = "agent", path: Optional[Path] = None
          ) -> Optional[Dict[str, Any]]:
    """Claim the FIFO-oldest "queued" entry (by queued_at; list order breaks
    ties). Sets in_progress / attempts+1 / started_at / claimed_by. Entries that
    are tailoring or terminal are never claimed. None when nothing is queued."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        best = None
        for e in data["jobs"]:
            if e.get("status") != "queued":
                continue
            if best is None or e.get("queued_at", "") < best.get("queued_at", ""):
                best = e
        if best is None:
            return None
        _normalize(best)                     # hand-edited entries: full schema
        best["status"] = "in_progress"
        best["attempts"] = int(best.get("attempts", 0)) + 1
        best["claimed_by"] = str(claimed_by or "")
        best["started_at"] = _now()
        best["updated_at"] = _now()
        _save(data, path)
        return dict(best)


def update(job_id: str, path: Optional[Path] = None, *,
           notes: Optional[str] = None, tab_note: Optional[str] = None,
           claimed_by: Optional[str] = None, ats: Optional[Dict[str, str]] = None,
           company: Optional[str] = None, title: Optional[str] = None,
           apply_url: Optional[str] = None, is_easy_apply: Optional[bool] = None,
           batch_id: Optional[str] = None) -> Dict[str, Any]:
    """Set the given descriptive fields (None = leave alone). `ats` merges the
    known keys into the entry's ats dict. Status transitions live in claim /
    finish / requeue, never here."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        for key, val in (("notes", notes), ("tab_note", tab_note),
                         ("claimed_by", claimed_by), ("company", company),
                         ("title", title), ("apply_url", apply_url),
                         ("batch_id", batch_id)):
            if val is not None:
                e[key] = str(val)
        if is_easy_apply is not None:
            e["is_easy_apply"] = bool(is_easy_apply)
        if ats:
            for k in ATS_KEYS:
                if k in ats and ats[k] is not None:
                    e["ats"][k] = str(ats[k])
        e["updated_at"] = _now()
        _save(data, path)
        return dict(e)


def add_missing(job_id: str, question: str, context: str = "",
                suggestion: str = "", path: Optional[Path] = None
                ) -> Dict[str, Any]:
    """Append one missing-answer item ({question, context, suggestion}) — the
    agent's "this form asked something the answer store can't cover" report."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        e["missing_answers"].append({"question": str(question),
                                     "context": str(context or ""),
                                     "suggestion": str(suggestion or "")})
        e["updated_at"] = _now()
        _save(data, path)
        return dict(e)


def finish(job_id: str, status: str, *, tab_note: str = "", record: str = "",
           notes: Optional[str] = None, path: Optional[Path] = None
           ) -> Dict[str, Any]:
    """Move an entry to a TERMINAL status (ready_to_submit | needs_human |
    failed) and stamp finished_at. tab_note carries "final URL | page title" so
    a human can find the parked tab; `record` is the application_record path."""
    if status not in TERMINAL:
        raise ValueError(
            f"finish() only accepts terminal statuses {sorted(TERMINAL)}, "
            f"not {status!r}")
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        e["status"] = status
        e["finished_at"] = _now()
        if tab_note:
            e["tab_note"] = str(tab_note)
        if record:
            e["artifacts"]["application_record"] = str(record)
        if notes is not None:
            e["notes"] = str(notes)
        e["updated_at"] = _now()
        _save(data, path)
        return dict(e)


def requeue(job_id: str, *, refresh_answers: bool = False,
            path: Optional[Path] = None) -> Dict[str, Any]:
    """Send an entry (any status) back to "queued": clears missing_answers /
    finished_at / tab_note / claimed_by, KEEPS attempts (the retry count is the
    point), re-stamps queued_at (a requeued job goes to the back of the FIFO).

    refresh_answers=True re-splices the folder's apply.md Standard-answers
    section from the current store (apply_data.refresh_standard_answers) —
    best-effort: a raised exception is logged to stderr and never fails the
    requeue itself.
    """
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        e["status"] = "queued"
        e["missing_answers"] = []
        e["finished_at"] = ""
        e["tab_note"] = ""
        e["claimed_by"] = ""
        e["queued_at"] = _now()
        e["updated_at"] = _now()
        _save(data, path)
        entry = dict(e)
    if refresh_answers:
        folder = entry.get("artifacts", {}).get("folder") or ""
        if folder:
            try:
                from resume_tailor import apply_data
                apply_data.refresh_standard_answers(Path(folder))
            except Exception as exc:  # never fail the requeue over the sheet
                print(f"apply_queue: refresh_standard_answers failed for "
                      f"{folder}: {exc}", file=sys.stderr)
    return entry


def remove(job_id: str, path: Optional[Path] = None) -> None:
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        e = _find(data, job_id)
        data["jobs"].remove(e)
        _save(data, path)


def clear_finished(path: Optional[Path] = None) -> int:
    """Drop every terminal entry; returns how many were removed."""
    with locked(path):
        data = load(path, quarantine=True)   # under locked(): may rename aside
        keep = [e for e in data["jobs"] if e.get("status") not in TERMINAL]
        removed = len(data["jobs"]) - len(keep)
        if removed:
            data["jobs"] = keep
            _save(data, path)
        return removed


# ── read-only views ──────────────────────────────────────────────────────────

def stats(path: Optional[Path] = None) -> Dict[str, int]:
    """{status: count} over every known status, plus "total". Lock-free."""
    data = load(path)
    out = {s: 0 for s in STATUSES}
    for e in data["jobs"]:
        s = e.get("status")
        if s in out:
            out[s] += 1
    out["total"] = len(data["jobs"])
    return out


def build_context(path: Optional[Path] = None) -> Dict[str, Any]:
    """The batch-run context the agent needs before draining the queue.

    signup_email comes from the master yaml (basics.email); inbox_url and
    batch_cap from the dashboard config.json's auto_apply_* keys — tolerantly,
    since SP3 hasn't added the Settings UI yet. Never anything secret-shaped.
    """
    try:
        from resume_tailor import assets
        email = str((assets.load_master().get("basics") or {}).get("email") or "")
    except Exception:
        email = ""
    try:
        cfg = json.loads(Path(CONFIG_JSON).read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    inbox = cfg.get("auto_apply_inbox_url")
    inbox = str(inbox).strip() if isinstance(inbox, str) and str(inbox).strip() \
        else "https://mail.google.com"
    try:
        cap = int(cfg.get("auto_apply_batch_cap", 10))
    except (TypeError, ValueError):
        cap = 10
    try:
        from resume_tailor import config as rt_config
        output_root = str(rt_config.OUTPUT_ROOT)
    except Exception:
        output_root = ""
    return {
        "signup_email": email,
        "inbox_url": inbox,
        "batch_cap": cap,
        "output_root": output_root,
        "queue_path": str(queue_path(path)),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _force_utf8_stdio() -> None:
    """Piped stdout/stderr on Windows default to cp1252, so any job title with
    an emoji/arrow would UnicodeEncodeError mid-verb — AFTER a claim already
    persisted its mutation, leaving the agent without the entry it now owns.
    Reconfigure both streams to UTF-8 up front; errors="replace" so printing
    can never raise, whatever the terminal."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _print_entry(e: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(e, indent=2, ensure_ascii=False))
    else:
        print(f"{e['job_posting_id']}  {e['status']:16} "
              f"{e['company']} — {e['title']}")


def main(argv: Optional[List[str]] = None) -> int:
    """Exit codes: 0 ok · 1 unexpected error (one line on stderr) · 2 unknown
    job id · 3 lock timeout · 4 claim on an empty queue. Every verb accepts
    --queue PATH to override the default."""
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="apply_queue",
        description="Batch auto-apply queue (never submits an application).")
    sub = ap.add_subparsers(dest="verb", required=True)

    def add(name, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--queue", metavar="PATH", default=None,
                       help="queue file (default: %%LOCALAPPDATA%%\\linkedin_watcher"
                            "\\apply_queue.json, or APPLY_QUEUE_PATH)")
        return p

    add("list", help="print every entry")

    p = add("claim", help="claim the FIFO-oldest queued entry")
    p.add_argument("--json", action="store_true", help="print the entry as JSON")
    p.add_argument("--by", default="agent", help="claimed_by tag")

    p = add("update", help="set notes / tab-note / ats fields on one entry")
    p.add_argument("job_id")
    p.add_argument("--notes")
    p.add_argument("--tab-note", dest="tab_note")
    p.add_argument("--claimed-by", dest="claimed_by")
    p.add_argument("--ats-domain", dest="ats_domain")
    p.add_argument("--ats-system", dest="ats_system", choices=ATS_SYSTEMS)
    p.add_argument("--ats-account-status", dest="ats_account_status")

    p = add("add-missing", help="record one unanswered form question")
    p.add_argument("job_id")
    p.add_argument("--question", required=True)
    p.add_argument("--context", default="")
    p.add_argument("--suggestion", default="")

    p = add("finish", help="park an entry in a terminal status")
    p.add_argument("job_id")
    p.add_argument("--status", required=True, choices=sorted(TERMINAL))
    p.add_argument("--tab-note", dest="tab_note", default="")
    p.add_argument("--record", default="", help="application_record path")
    p.add_argument("--notes", default=None)

    p = add("requeue", help="send an entry back to queued")
    p.add_argument("job_id")
    p.add_argument("--refresh-answers", action="store_true",
                   help="re-splice the folder's apply.md standard answers")

    p = add("remove", help="delete an entry")
    p.add_argument("job_id")

    add("stats", help="per-status counts")

    p = add("context", help="batch-run context for the agent")
    p.add_argument("--json", action="store_true")

    p = add("enqueue", help="add one entry (tests / manual use)")
    p.add_argument("--job-id", required=True, dest="job_id")
    p.add_argument("--company", default="")
    p.add_argument("--title", default="")
    p.add_argument("--url", default="")
    p.add_argument("--easy-apply", action="store_true", dest="easy_apply")
    p.add_argument("--batch-id", dest="batch_id", default="")
    p.add_argument("--status", default="queued", choices=sorted(STATUSES))

    args = ap.parse_args(argv)
    qp = Path(args.queue) if args.queue else None

    try:
        if args.verb == "list":
            for e in load(qp)["jobs"]:
                _print_entry(e, as_json=False)
        elif args.verb == "claim":
            got = claim(claimed_by=args.by, path=qp)
            if got is None:
                print("queue empty: nothing to claim", file=sys.stderr)
                return 4
            _print_entry(got, as_json=args.json)
        elif args.verb == "update":
            ats = {}
            if args.ats_domain is not None:
                ats["domain"] = args.ats_domain
            if args.ats_system is not None:
                ats["system"] = args.ats_system
            if args.ats_account_status is not None:
                ats["account_status"] = args.ats_account_status
            update(args.job_id, path=qp, notes=args.notes,
                   tab_note=args.tab_note, claimed_by=args.claimed_by,
                   ats=ats or None)
        elif args.verb == "add-missing":
            add_missing(args.job_id, args.question, context=args.context,
                        suggestion=args.suggestion, path=qp)
        elif args.verb == "finish":
            finish(args.job_id, args.status, tab_note=args.tab_note,
                   record=args.record, notes=args.notes, path=qp)
        elif args.verb == "requeue":
            requeue(args.job_id, refresh_answers=args.refresh_answers, path=qp)
        elif args.verb == "remove":
            remove(args.job_id, path=qp)
        elif args.verb == "stats":
            for status, count in stats(path=qp).items():
                print(f"{status:16} {count}")
        elif args.verb == "context":
            ctx = build_context(path=qp)
            if args.json:
                print(json.dumps(ctx, indent=2, ensure_ascii=False))
            else:
                for k, v in ctx.items():
                    print(f"{k:14} {v}")
        elif args.verb == "enqueue":
            entry = new_entry(args.job_id, company=args.company,
                              title=args.title, apply_url=args.url,
                              is_easy_apply=args.easy_apply,
                              batch_id=args.batch_id, status=args.status)
            stored = enqueue(entry, path=qp)
            _print_entry(stored, as_json=False)
    except UnknownJobError as exc:
        print(f"apply_queue: {exc.args[0]}", file=sys.stderr)
        return 2
    except QueueLockTimeout as exc:
        print(f"apply_queue: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:   # anything unexpected: one line, documented exit 1
        print(f"apply_queue: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
