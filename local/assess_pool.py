"""The parallel difficulty check's profile copies (cycle 22, SP1).

Two browsers on one profile can lose its saved sign-ins, so a check of several
jobs at once gives each worker its own temporary copy of the auto-apply
profile, one slot folder per concurrent worker, under
`%LOCALAPPDATA%\\linkedin_watcher\\assess_profiles`. The copies hold the
sign-in cookies, so `sweep_slots` clears the root before a run starts and
again when it ends, waiting up to FINAL_SWEEP_S for a browser still closing.
A copy that outlives its run (a console closed with X, a browser that would
not let go) is swept by the next holder of the real profile's sentinel:
`apply_run.launch_profile` on the real profile (a drain, `one`, the sign-in,
the one-job check) and the dashboard at start (`sweep_if_free`). Nothing
sweeps without holding that sentinel, so a live pool's slots are never
touched.

`snapshot_profile` leaves out what a browser rebuilds (its caches) and what
only a running browser's own copy may hold (its lock files). The sign-ins are
in what stays: the cookies, Local State, Preferences and the storage folders.

`run_pool` (SP2) is the coordinator: it holds the real profile for the whole
run, copies it into one slot per concurrent worker, and runs each job as its
own `apply_assess.py --worker` process on a free slot, printing one line per
job as each one finishes.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping

SLOT_PREFIX = "slot-"
ROOT_NAME = "assess_profiles"
SWEEP_TRIES = 3
SWEEP_WAIT_S = 0.3
FINAL_SWEEP_S = 12.0        # how long the end of a run waits for a browser still closing
FINAL_SWEEP_POLL_S = 0.5
LEFT_BEHIND = ("The profile copy {folder} was not deleted (a browser still had it open). It "
               "holds your sign-ins, and it is deleted the next time the auto-apply browser "
               "opens or the dashboard starts.")

# --- SP2: the pool ---------------------------------------------------------------------
PARALLEL_DEFAULT = 10       # `auto_apply_check_parallel`'s default
PARALLEL_MAX = 10
SCRIPT = Path(__file__).resolve().parent / "apply_assess.py"
JOIN_POLL_S = 0.2           # the main thread's wait between looks, short so Ctrl+C lands
CANCEL_WAIT_S = 10.0        # after Ctrl+C, how long each slot's thread gets to end
STDERR_MAX = 200            # the most of a stopped worker's last stderr line kept
WORKER_LOG_MAX = 60         # the most of a worker's warning and traceback lines shown
START_LINE = "Checking {jobs} jobs, up to {at_once} at once, each in its own browser window."
NO_COPY = ("The auto-apply profile could not be copied for the parallel check ({why}), so the "
           "difficulty check stops here.")
INTERRUPTED = ("apply_assess: stopped (Ctrl+C). The checks still running were ended, and "
               "their jobs keep their earlier result.")
# A worker's own logging line (`apply_assess._main`'s basicConfig format), which
# never says why the worker stopped.
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \S+ "
                       r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) ")
_SHOWN_LEVELS = ("WARNING", "ERROR", "CRITICAL")

# Folders (matched by name, at any depth) a browser fills again by itself.
CACHE_DIRS = frozenset(name.lower() for name in (
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache", "Crashpad", "component_crx_cache",
    "extensions_crx_cache", "BrowserMetrics", "Media Cache", "ScriptCache", "CacheStorage",
    "optimization_guide_model_store"))
# What a running browser holds open: Chrome's lock files and the sentinel.
LOCK_NAMES = frozenset({"lockfile"})
LOCK_PREFIXES = ("singleton",)
LOCK_SUFFIXES = (".inuse",)

log = logging.getLogger("assess_pool")


def slot_root() -> Path:
    """Where the slot folders live, derived from LOCALAPPDATA the way
    `profile_lock.default_profile_dir` is."""
    appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / ROOT_NAME


def slot_dir(number: int, root: Path | None = None) -> Path:
    """Slot `number` (1 up) under `root` (the slot root unless given)."""
    return (Path(root) if root else slot_root()) / f"{SLOT_PREFIX}{int(number)}"


def skipped(name: str, *, is_dir: bool) -> bool:
    """Does the copy leave this entry out: a cache folder or a lock file?"""
    low = name.lower()
    if is_dir:
        return low in CACHE_DIRS
    return low in LOCK_NAMES or low.startswith(LOCK_PREFIXES) or low.endswith(LOCK_SUFFIXES)


def snapshot_profile(src: Path, dest: Path) -> list[str]:
    """Copy the profile folder `src` into `dest`, leaving out its caches and
    lock files (`skipped`). A file that will not copy is logged and left out;
    the list returned is those files' paths (empty when the copy is whole).
    `dest` is made, and must not hold a copy already. Raises FileNotFoundError
    when `src` is not a folder."""
    src, dest = Path(src), Path(dest)
    if not src.is_dir():
        raise FileNotFoundError(f"no browser profile at {src}")
    failed: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for here, dirs, files in os.walk(src):
        rel = Path(here).relative_to(src)
        dirs[:] = [d for d in dirs if not skipped(d, is_dir=True)]
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if skipped(name, is_dir=False):
                continue
            try:
                shutil.copy2(Path(here) / name, dest / rel / name)
            except OSError as e:
                failed.append(str(Path(here) / name))
                log.warning("profile copy: %s was not copied (%s)", rel / name,
                            type(e).__name__)
    return failed


def _writable_then_retry(func, path, error) -> None:
    """`shutil.rmtree`'s error hook: a read-only entry is made writable and
    removed again; any other error is raised for the sweep to count."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _delete(folder: Path, tries: int, wait_s: float) -> OSError | None:
    """Delete one slot folder, tried `tries` times `wait_s` apart: None once
    it is gone, else the last error."""
    for attempt in range(max(1, tries)):
        try:
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder, onexc=_writable_then_retry)
            else:
                folder.unlink()
        except FileNotFoundError:
            return None
        except OSError as e:
            if attempt + 1 < max(1, tries):
                time.sleep(wait_s)
                continue
            return e
        else:
            return None
    return None


def leftovers(root: Path | None = None) -> list[Path]:
    """The slot folders under `root` (the slot root unless given), read only."""
    root = Path(root) if root else slot_root()
    try:
        return sorted(p for p in root.iterdir() if p.name.startswith(SLOT_PREFIX))
    except OSError:
        return []


def sweep_slots(root: Path | None = None, *, tries: int | None = None,
                wait_s: float | None = None, patience_s: float = 0.0) -> list[Path]:
    """Delete every leftover slot folder under `root` (the slot root unless
    given). Best effort: a folder that will not delete (a browser still has a
    file open) is tried `tries` times, `wait_s` apart (SWEEP_TRIES and
    SWEEP_WAIT_S unless given), and the whole sweep is tried again every
    FINAL_SWEEP_POLL_S for up to `patience_s`. What is still there is logged
    and returned (empty when the root is clean). Never raises.

    Only a holder of the real profile's sentinel calls it: a pool holds that
    sentinel for its whole run, so no pool's slots are live then."""
    tries = SWEEP_TRIES if tries is None else tries
    wait_s = SWEEP_WAIT_S if wait_s is None else wait_s
    deadline = time.monotonic() + max(0.0, patience_s)
    errors: dict[Path, OSError | None] = {}
    while True:
        errors = {folder: _delete(folder, tries, wait_s) for folder in leftovers(root)}
        left = [folder for folder, error in errors.items() if error is not None]
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(FINAL_SWEEP_POLL_S)
    for folder in left:
        log.warning("profile copy %s was not deleted (%s)", folder.name,
                    type(errors[folder]).__name__)
    return left


def is_real_profile(profile_dir: Path) -> bool:
    """Is `profile_dir` the real auto-apply profile, whose sentinel guards the
    slot root? A slot, or any other folder, is not."""
    import profile_lock
    try:
        return Path(profile_dir).resolve() == profile_lock.default_profile_dir().resolve()
    except OSError:
        return False


def sweep_if_free(*, wait_s: float = 0.0) -> list[Path] | None:
    """Sweep leftover copies when no browser holds the real profile: its
    sentinel is taken for the sweep and given back. None when another holder
    has it (a live pool among them), and a clean root takes no hold. The
    dashboard calls this at start, off its UI thread."""
    import profile_lock
    if not leftovers():
        return []
    guard = profile_lock.hold(profile_lock.default_profile_dir(), wait_s=wait_s)
    if guard is None:
        return None
    try:
        return sweep_slots(tries=1)
    finally:
        guard.release()


# --- SP2: the coordinator -------------------------------------------------------------------

def parallel_setting(value: Any, default: int = PARALLEL_DEFAULT) -> int:
    """How many checks run at once: `value` (the `--parallel` flag or the
    `auto_apply_check_parallel` setting) as a whole number clamped to 1 to
    PARALLEL_MAX; `default` for anything that is not a number."""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(PARALLEL_MAX, number))


def worker_argv(job_id: str, slot: Path, *, queue_path: Path | None = None,
                headless: bool = False, verbose: bool = False) -> list[str]:
    """The command line of one job's worker on `slot`."""
    argv = [sys.executable, str(SCRIPT), "--worker", "--profile", str(slot)]
    if queue_path:
        argv += ["--queue", str(queue_path)]
    if headless:
        argv.append("--headless")
    if verbose:
        argv.append("--verbose")
    return [*argv, str(job_id)]


def spawn_worker(job_id: str, slot: Path, *, queue_path: Path | None = None,
                 headless: bool = False, verbose: bool = False) -> subprocess.Popen:
    """Start one job's worker (`worker_argv`) with its output captured. Its
    browser window shows as the one-job check's does; only the worker's own
    console lines are captured, read as UTF-8."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.Popen(worker_argv(job_id, slot, queue_path=queue_path, headless=headless,
                                        verbose=verbose),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=env)


def stopped_why(stderr: str, returncode: Any) -> str:
    """Why a worker that printed no result line stopped: its last stderr line
    that is not one of its logging lines, else its exit code."""
    for line in reversed(str(stderr or "").splitlines()):
        line = line.strip()
        if line and not _LOG_LINE.match(line):
            return f"the worker stopped: {line[:STDERR_MAX]}"
    if returncode is None:
        return "the worker stopped"
    return f"the worker stopped: exit code {returncode}"


def worker_log(stderr: str, *, drop: str = "") -> list[str]:
    """The lines of a worker's stderr the console shows when its job did not
    score, as the one-job check shows them: its WARNING, ERROR and CRITICAL
    log lines with the lines that follow each (a traceback's frames), and any
    line before its first log line (a crash before logging started). `drop`
    (the sentence the pool already printed) is left out; at most
    WORKER_LOG_MAX lines, the last ones."""
    kept: list[str] = []
    show = True
    for line in str(stderr or "").splitlines():
        level = _LOG_LINE.match(line)
        if level:
            show = level.group(1) in _SHOWN_LEVELS
        if show and line.strip() and line.strip() != drop:
            kept.append(line.rstrip())
    return kept[-WORKER_LOG_MAX:]


class _Pool:
    """One parallel check: the jobs not started yet, the live workers, the
    running Jev totals and the stop. Every print and every count is taken
    under `lock`, so lines never interleave and the totals add up."""

    def __init__(self, chosen: list[Mapping[str, Any]], *, spawn: Callable,
                 queue_path: Path | None, headless: bool, log: logging.Logger,
                 verbose: bool = False):
        self.pending = deque(chosen)
        self.jobs = len(chosen)
        self.spawn = spawn
        self.queue_path = queue_path
        self.headless = headless
        self.verbose = verbose
        self.log = log
        self.lock = threading.Lock()
        self.live: set = set()
        self.done = 0
        self.requests = 0
        self.usd = 0.0
        self.stopped = False        # Jev down or a window closed: no new job starts
        self.cancelled = False      # Ctrl+C: the live workers are ended, nothing is noted
        self.refused = False        # a worker refused (no browser started): said once, stop
        self.threads: list[threading.Thread] = []

    # --- the slots' threads -------------------------------------------------------------

    def start(self, slots: list[Path]) -> None:
        for k, slot in enumerate(slots, 1):
            t = threading.Thread(target=self._slot, args=(slot,), daemon=True,
                                 name=f"assess-slot-{k}")
            self.threads.append(t)
            t.start()

    def wait(self) -> None:
        while any(t.is_alive() for t in self.threads):
            for t in self.threads:
                t.join(JOIN_POLL_S)

    def cancel(self) -> None:
        """Ctrl+C: start nothing more, end every live worker and give each
        slot's thread a moment to see its worker gone."""
        with self.lock:
            self.cancelled = self.stopped = True
            live = list(self.live)
        for proc in live:
            try:
                proc.terminate()
            except Exception:       # noqa: BLE001  (it ended on its own)
                pass
        for t in self.threads:
            t.join(CANCEL_WAIT_S)

    def _slot(self, slot: Path) -> None:
        while True:
            with self.lock:
                if self.stopped or not self.pending:
                    return
                entry = self.pending.popleft()
            try:
                self._job(entry, slot)
            except Exception as e:      # noqa: BLE001  (one job's line; the next job runs)
                self.log.exception("job %s: the parallel check failed",
                                   entry.get("job_posting_id"))
                self._failed(entry, f"the check failed ({type(e).__name__})")

    def _job(self, entry: Mapping[str, Any], slot: Path) -> None:
        import apply_assess
        jid = str(entry.get("job_posting_id") or "")
        try:
            proc = self.spawn(jid, slot, queue_path=self.queue_path, headless=self.headless,
                              verbose=self.verbose)
        except Exception as e:      # noqa: BLE001  (no worker: this job's line says so)
            self.log.error("job %s: the worker did not start (%s)", jid, type(e).__name__)
            self._failed(entry, f"the worker did not start ({type(e).__name__})")
            return
        with self.lock:
            self.live.add(proc)
            late = self.cancelled
        if late:                    # Ctrl+C came while it started
            proc.terminate()
        try:
            out, err = proc.communicate()
        finally:
            with self.lock:
                self.live.discard(proc)
        if err:
            self.log.debug("job %s: the worker's log:\n%s", jid, err.rstrip())
        if self.cancelled:
            return
        got = apply_assess.parse_result_line(out)
        if got is None:
            self._failed(entry, stopped_why(err, getattr(proc, "returncode", None)),
                         detail=self._detail(err))
            return
        why = str(got.get("why") or "")
        detail = [] if got.get("outcome") == "scored" else self._detail(err, drop=why)
        if got.get("refusal"):
            self._refused(got, detail)
        elif got.get("failed"):
            self._failed(entry, why or "the worker failed", got, detail=detail)
        else:
            self._report(entry, got, detail)

    def _detail(self, err: str, *, drop: str = "") -> list[str]:
        """The worker's warnings and traceback for the console (`worker_log`);
        none when this run logs at DEBUG, which relays the whole log already."""
        if self.log.isEnabledFor(logging.DEBUG):
            return []
        return worker_log(err, drop=drop)

    def _refused(self, got: Mapping[str, Any], detail: list[str] = ()) -> None:
        """A worker's refusal (its result marked `refusal`: no browser
        started, a gate) stops the pool: no new job starts, and its sentence
        is printed once, however many running workers meet it."""
        import apply_assess
        with self.lock:
            first = not self.refused
            self.refused = self.stopped = True
            self.requests += int(got.get("requests") or 0)
            self.usd += float(got.get("usd") or 0.0)
            if first and not self.cancelled:
                apply_assess._say(str(got.get("why") or "the check was refused"))
                _show(detail)

    # --- the lines ----------------------------------------------------------------------

    def _line(self, entry: Mapping[str, Any], text: Callable[[str], str],
              got: Mapping[str, Any] | None = None, *, stop: bool = False,
              detail: list[str] = ()) -> None:
        """Print one finished job's line, numbered in finish order, with the
        totals including its own result, then its worker's `detail` lines on
        stderr; `stop` starts no new job after it."""
        import apply_assess
        name = f"{entry.get('company') or '?'} / {entry.get('title') or '?'}"
        with self.lock:
            if self.cancelled:
                return
            self.done += 1
            if got is not None:
                self.requests += int(got.get("requests") or 0)
                self.usd += float(got.get("usd") or 0.0)
            if stop:
                self.stopped = True
            running = f"Jev so far: {self.requests} request(s), ${self.usd:.4f}"
            apply_assess._say(f"[{self.done}/{self.jobs}] {name}: {text(running)}")
            _show(detail)

    def _failed(self, entry: Mapping[str, Any], why: str,
                got: Mapping[str, Any] | None = None, *, detail: list[str] = ()) -> None:
        """A job its worker did not check and did not note (no worker, no
        result line, a result marked `failed`: the worker crashed or its slot
        stayed busy): today's not-checked line, and the failure noted on its
        queue entry (the earlier result stays)."""
        import apply_queue
        if self.cancelled:
            return
        self._line(entry, lambda running: f"not checked ({why}). {running}", got,
                   detail=detail)
        jid = str(entry.get("job_posting_id") or "")
        try:
            apply_queue.note_difficulty_failure(jid, why, path=self.queue_path)
        except apply_queue.UnknownJobError:
            pass
        except Exception as e:      # noqa: BLE001  (a busy queue file: the line said it)
            self.log.warning("job %s: the failure was not noted on the queue (%s)", jid,
                             type(e).__name__)

    def _report(self, entry: Mapping[str, Any], got: Mapping[str, Any],
                detail: list[str] = ()) -> None:
        """A worker's result, in `apply_assess.run`'s words. The worker stored
        the difficulty, or noted the failure, on the queue itself."""
        outcome, why = got.get("outcome"), str(got.get("why") or "")
        if outcome == "scored":
            self._line(entry, lambda running: f"{got.get('score')}/10, {got.get('band')}. "
                                              f"{running}", got)
        elif outcome in ("outage", "closed"):
            self._line(entry, lambda running: f"{why}; the check stops here", got, stop=True,
                       detail=detail)
        elif why == "left the queue while it was checked":
            self._line(entry, lambda running: f"{why}. {running}", got, detail=detail)
        else:
            self._line(entry, lambda running: f"not checked ({why}). {running}", got,
                       detail=detail)


def _show(lines) -> None:
    """A worker's log lines on stderr, as the one-job check prints them."""
    for line in lines:
        try:
            print(line, file=sys.stderr, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stderr, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc), file=sys.stderr, flush=True)


def say_left(left: list[Path]) -> None:
    """One plain line per profile copy a sweep could not delete."""
    import apply_assess
    for folder in left:
        apply_assess._say(LEFT_BEHIND.format(folder=folder))


def run_pool(chosen: list[Mapping[str, Any]], *, parallel: int, profile: Path,
             unknown: list[str] | tuple = (), queue_path: Path | None = None,
             headless: bool = False, verbose: bool = False, spawn: Callable | None = None,
             root: Path | None = None, log: logging.Logger | None = None) -> int:
    """Check the `chosen` queue entries up to `parallel` at once, each job a
    worker process on its own copy of `profile`. `apply_assess._main` runs the
    gates (the mode, the Jev refusal, the profile busy) and comes here only for
    two or more jobs and `parallel` above 1; this reads the Apply Answers file
    and takes the real profile's sentinel, held for the whole run, so a drain,
    a sign-in or another check refuses meanwhile. The copies (slots under
    `root`, the slot root unless given) are swept before and after, the last
    sweep waiting up to FINAL_SWEEP_S for a browser still closing; a copy it
    still cannot delete is named in a line (LEFT_BEHIND). `spawn` starts one
    worker (`spawn_worker` unless given; tests pass a fake), with `--verbose`
    when `verbose`.

    Exit 0 when every job ran, 1 when Jev went down, a window was closed, a
    worker refused (no browser started), Ctrl+C was pressed or the profile
    could not be copied, 2 when it refuses
    (the profile in use, the Apply Answers file damaged)."""
    import apply_assess
    import profile_lock
    from resume_tailor import apply_answers
    log = log or logging.getLogger("apply_assess")
    try:
        apply_answers.load()
    except apply_answers.AnswerStoreError as e:
        apply_assess._say(f"The Apply Answers file is damaged ({e.path}): {e.reason}. Open "
                          f"the dashboard's Apply Answers tab to restore the backup.")
        return 2
    for jid in unknown:
        apply_assess._say(f"apply_assess: job {jid} is not in the queue")
    profile = Path(profile)
    pool = _Pool(list(chosen), spawn=spawn or spawn_worker, queue_path=queue_path,
                 headless=headless, log=log, verbose=verbose)
    guard = profile_lock.hold(profile)
    if guard is None:
        print(apply_assess.PROFILE_BUSY, file=sys.stderr)
        return 2
    try:
        at_once = max(1, min(int(parallel), len(chosen)))
        apply_assess._say(START_LINE.format(jobs=len(chosen), at_once=at_once))
        say_left(sweep_slots(root))
        profile.mkdir(parents=True, exist_ok=True)      # as the one-job check opens it
        slots = []
        for k in range(1, at_once + 1):
            slot = slot_dir(k, root)
            try:
                missed = snapshot_profile(profile, slot)
            except OSError as e:
                log.error("slot-%d: the profile copy failed (%s)", k, type(e).__name__)
                apply_assess._say(NO_COPY.format(why=type(e).__name__))
                return 1
            if missed:
                log.warning("slot-%d: %d file(s) of the profile were not copied", k,
                            len(missed))
            slots.append(slot)
        pool.start(slots)
        pool.wait()
        return 1 if pool.stopped else 0
    except KeyboardInterrupt:
        pool.cancel()
        apply_assess._say(INTERRUPTED)
        return 1
    finally:
        try:
            try:
                left = sweep_slots(root, patience_s=FINAL_SWEEP_S)
            except KeyboardInterrupt:       # a second Ctrl+C cuts the wait short
                left = leftovers(root)
            say_left(left)
        finally:
            guard.release()
