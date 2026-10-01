"""The parallel difficulty check's profile copies (cycle 22, SP1): what the
snapshot leaves out and keeps, and the sweep of leftover slot folders. Fake
profile trees under tmp_path; the real profile is never read."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import assess_pool as ap


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


KEPT = ["Local State", "Default/Preferences", "Default/Network/Cookies", "Default/Login Data",
        "Default/Local Storage/leveldb/000003.log", "Default/IndexedDB/https_x/1.leveldb",
        "Default/Sessions/Session_1", "First Run"]
SKIPPED = ["Default/Cache/Cache_Data/f_000001", "Default/Code Cache/js/index",
           "Default/GPUCache/data_0", "ShaderCache/GPUCache/data_0", "GrShaderCache/data_0",
           "DawnCache/data_0", "GraphiteDawnCache/data_0", "Default/DawnWebGPUCache/data_0",
           "Default/Service Worker/CacheStorage/1/entry", "Default/Service Worker/ScriptCache/a",
           "Crashpad/settings.dat", "component_crx_cache/x.crx", "BrowserMetrics/m.pma",
           "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket",
           "Default/lockfile"]


@pytest.fixture
def fake_profile(tmp_path):
    src = tmp_path / "browser_profile"
    for rel in KEPT:
        _write(src, rel, rel)
    for rel in SKIPPED:
        _write(src, rel, rel)
    _write(tmp_path, "browser_profile.inuse")           # the sentinel sits beside the profile
    return src


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_the_snapshot_keeps_the_sign_ins_and_skips_caches_and_locks(fake_profile, tmp_path):
    dest = tmp_path / "slots" / "slot-1"
    assert ap.snapshot_profile(fake_profile, dest) == []
    got = _files(dest)
    assert got == set(KEPT)
    for rel in KEPT:
        assert (dest / rel).read_text(encoding="utf-8") == rel      # the bytes came along
    assert not any(part.lower() in ap.CACHE_DIRS
                   for rel in got for part in Path(rel).parts[:-1])


def test_the_snapshot_leaves_the_source_whole(fake_profile, tmp_path):
    before = _files(fake_profile)
    ap.snapshot_profile(fake_profile, tmp_path / "copy")
    assert _files(fake_profile) == before == set(KEPT) | set(SKIPPED)


def test_an_inuse_file_is_a_lock_wherever_it_sits(tmp_path):
    src = tmp_path / "p"
    _write(src, "Default/Preferences")
    _write(src, "Default/x.inuse")
    _write(src, "Default/Cache.txt")                    # a cache-like name is not a cache folder
    ap.snapshot_profile(src, tmp_path / "d")
    assert _files(tmp_path / "d") == {"Default/Preferences", "Default/Cache.txt"}


def test_a_cache_name_matches_by_folder_not_by_file(tmp_path):
    src = tmp_path / "p"
    _write(src, "Default/Cache")                        # a FILE named Cache is kept
    ap.snapshot_profile(src, tmp_path / "d")
    assert _files(tmp_path / "d") == {"Default/Cache"}


def test_empty_folders_come_along(tmp_path):
    src = tmp_path / "p"
    (src / "Default" / "Extensions").mkdir(parents=True)
    ap.snapshot_profile(src, tmp_path / "d")
    assert (tmp_path / "d" / "Default" / "Extensions").is_dir()


def test_a_missing_profile_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ap.snapshot_profile(tmp_path / "nope", tmp_path / "d")


def test_a_file_that_will_not_copy_is_listed_and_the_rest_copies(fake_profile, tmp_path,
                                                                 monkeypatch):
    real = ap.shutil.copy2

    def picky(src, dst, *a, **kw):
        if Path(src).name == "Login Data":
            raise PermissionError("in use")
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(ap.shutil, "copy2", picky)
    failed = ap.snapshot_profile(fake_profile, tmp_path / "d")
    assert [Path(p).name for p in failed] == ["Login Data"]
    assert _files(tmp_path / "d") == set(KEPT) - {"Default/Login Data"}


def test_the_slot_root_follows_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ap.slot_root() == tmp_path / "linkedin_watcher" / "assess_profiles"
    assert ap.slot_dir(3) == ap.slot_root() / "slot-3"
    assert ap.slot_dir(2, tmp_path / "r") == tmp_path / "r" / "slot-2"


def test_the_slot_root_is_beside_the_profile_not_in_it(monkeypatch, tmp_path):
    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ap.slot_root().parent == profile_lock.default_profile_dir().parent
    assert profile_lock.default_profile_dir() not in ap.slot_root().parents


# --- the sweep -----------------------------------------------------------------------------

def _slots(root: Path, n: int = 3) -> list[Path]:
    made = []
    for k in range(1, n + 1):
        _write(root, f"slot-{k}/Default/Network/Cookies", "cookies")
        _write(root, f"slot-{k}/Local State", "{}")
        made.append(root / f"slot-{k}")
    return made


def test_the_sweep_deletes_every_leftover_slot(tmp_path):
    root = tmp_path / "assess_profiles"
    slots = _slots(root)
    assert ap.sweep_slots(root) == []
    assert not any(s.exists() for s in slots)


def test_the_sweep_leaves_what_is_not_a_slot(tmp_path):
    root = tmp_path / "assess_profiles"
    _slots(root, 1)
    _write(root, "notes.txt")
    assert ap.sweep_slots(root) == []
    assert [p.name for p in root.iterdir()] == ["notes.txt"]


def test_the_sweep_of_a_missing_root_is_a_no_op(tmp_path):
    assert ap.sweep_slots(tmp_path / "never_made") == []


def test_the_sweep_removes_a_read_only_file(tmp_path):
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    os.chmod(slot / "Local State", 0o444)
    assert ap.sweep_slots(root, wait_s=0) == []
    assert not slot.exists()


def test_the_sweep_retries_a_slot_that_will_not_delete_then_gives_it_up(tmp_path, monkeypatch):
    root = tmp_path / "assess_profiles"
    slots = _slots(root, 2)
    real = ap.shutil.rmtree
    calls = []

    def stubborn(path, *a, **kw):
        calls.append(Path(path).name)
        if Path(path).name == "slot-1":
            raise PermissionError("a browser still has it open")
        return real(path, *a, **kw)
    monkeypatch.setattr(ap.shutil, "rmtree", stubborn)
    assert ap.sweep_slots(root, tries=3, wait_s=0) == [slots[0]]    # logged, never raised
    assert calls.count("slot-1") == 3 and calls.count("slot-2") == 1
    assert slots[0].exists() and not slots[1].exists()


def test_the_sweep_succeeds_on_a_retry(tmp_path, monkeypatch):
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    real = ap.shutil.rmtree
    tries = []

    def flaky(path, *a, **kw):
        tries.append(1)
        if len(tries) < 2:
            raise PermissionError("a browser is still closing")
        return real(path, *a, **kw)
    monkeypatch.setattr(ap.shutil, "rmtree", flaky)
    assert ap.sweep_slots(root, tries=3, wait_s=0) == []
    assert len(tries) == 2 and not slot.exists()


@pytest.mark.skipif(os.name != "nt", reason="a file held open blocks delete on Windows")
def test_the_sweep_keeps_a_slot_with_a_locked_file_and_deletes_it_once_free(tmp_path):
    import ctypes
    from ctypes import wintypes
    root = tmp_path / "assess_profiles"
    (slot,) = _slots(root, 1)
    held = slot / "Default" / "Network" / "Cookies"
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(str(held), 0x80000000, 0, None, 3, 0x80, None)     # read, no sharing
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert ap.sweep_slots(root, tries=2, wait_s=0) == [slot]
        assert held.exists()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert ap.sweep_slots(root, wait_s=0) == []
    assert not slot.exists()


# === cycle 22 SP2: the pool (coordinator) ===================================================
#
# A fake spawner stands in for the worker processes: no browser, no Jev, no
# child Python. The profile, the slot root and the queue are tmp_path fakes.

import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import apply_assess as aa  # noqa: E402
import apply_queue  # noqa: E402
import profile_lock  # noqa: E402

COMPANIES = ["Fabrikam", "Contoso", "Northwind", "Litware", "Tailspin", "Adatum", "Proseware"]


def _result(jid, outcome="scored", *, score=2, band="Queue it", why="", requests=1, usd=0.01):
    return aa.result_line(aa.worker_result({
        "job_id": jid, "outcome": outcome, "score": score if outcome == "scored" else None,
        "band": band if outcome == "scored" else "", "why": why, "requests": requests,
        "usd": usd}))


class FakeProc:
    """A worker process: `communicate` runs `act` (which returns stdout and
    stderr); `terminate` sets `ended` for an `act` that waits on it."""

    def __init__(self, act):
        self.act = act
        self.ended = threading.Event()
        self.terminated = False
        self.returncode = None

    def communicate(self):
        out, err = self.act(self)
        if self.returncode is None:
            self.returncode = 1 if self.terminated else 0
        return out, err

    def terminate(self):
        self.terminated = True
        self.ended.set()


class FakeSpawner:
    """Records every start and the most workers alive at once; `behave(jid,
    proc)` gives a job's (stdout, stderr), a scored result line unless set."""

    def __init__(self, behave=None, *, hold_s=0.0):
        self.behave = behave or (lambda jid, proc: (f"[1/1] x\n{_result(jid)}\n", ""))
        self.hold_s = hold_s
        self.lock = threading.Lock()
        self.calls, self.procs = [], []
        self.live = self.most = 0
        self.slots_busy: set = set()
        self.overlap = False

    def __call__(self, job_id, slot, *, queue_path=None, headless=False):
        def act(proc):
            with self.lock:
                self.live += 1
                self.most = max(self.most, self.live)
                self.overlap = self.overlap or slot in self.slots_busy
                self.slots_busy.add(slot)
            try:
                time.sleep(self.hold_s)
                return self.behave(job_id, proc)
            finally:
                with self.lock:
                    self.live -= 1
                    self.slots_busy.discard(slot)
        proc = FakeProc(act)
        with self.lock:
            self.calls.append((job_id, Path(slot), queue_path, headless))
            self.procs.append(proc)
        return proc


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A fake signed-in profile, a slot root and a queue of seven jobs (ids 1
    to 7, Fabrikam first); `pool.run(spawner, ...)` runs `run_pool` on them."""
    monkeypatch.setattr(profile_lock, "HOLD_WAIT_S", 0)
    monkeypatch.setattr(ap, "JOIN_POLL_S", 0.01)
    profile = tmp_path / "appdata" / "linkedin_watcher" / "browser_profile"
    for rel in ("Local State", "Default/Network/Cookies", "Default/Cache/data_0", "lockfile"):
        _write(profile, rel, rel)
    queue = tmp_path / "queue.json"
    for i, company in enumerate(COMPANIES, 1):
        apply_queue.enqueue(apply_queue.new_entry(str(i), company=company, title="Analyst",
                                                  apply_url=f"https://jobs.lever.co/x/{i}"),
                            path=queue)

    class _Pool:
        pass
    p = _Pool()
    p.profile, p.queue, p.root = profile, queue, tmp_path / "slots"

    def entries(ids=None):
        jobs = apply_queue.load(queue)["jobs"]
        return [e for e in jobs if ids is None or e["job_posting_id"] in ids]

    def run(spawner, *, parallel=3, ids=None, **kw):
        return ap.run_pool(entries(ids), parallel=parallel, profile=profile, queue_path=queue,
                           spawn=spawner, root=p.root, **kw)
    p.entries, p.run = entries, run
    return p


def _out(capsys):
    return capsys.readouterr().out.strip().splitlines()


def _unnumbered(lines):
    return sorted(re.sub(r"^\[\d+/(\d+)\] ", r"[k/\1] ", x) for x in lines)


def test_the_pool_never_runs_more_workers_than_its_slots(pool, capsys):
    spawner = FakeSpawner(hold_s=0.05)
    assert pool.run(spawner, parallel=3) == 0
    assert spawner.most == 3 and not spawner.overlap
    assert sorted(c[0] for c in spawner.calls) == [str(i) for i in range(1, 8)]
    assert {c[1].name for c in spawner.calls} == {"slot-1", "slot-2", "slot-3"}
    assert all(c[1].parent == pool.root for c in spawner.calls)
    assert {(c[2], c[3]) for c in spawner.calls} == {(pool.queue, False)}
    lines = _out(capsys)
    assert lines[0] == "Checking 7 jobs, up to 3 at once, each in its own browser window."
    assert sorted(x[:5] for x in lines[1:]) == [f"[{k}/7]" for k in range(1, 8)]


def test_the_pool_takes_no_more_slots_than_jobs(pool, capsys):
    spawner = FakeSpawner(hold_s=0.1)
    assert pool.run(spawner, parallel=10, ids={"1", "2"}, headless=True) == 0
    assert {c[1].name for c in spawner.calls} == {"slot-1", "slot-2"}
    assert {c[3] for c in spawner.calls} == {True}
    assert _out(capsys)[0] == ("Checking 2 jobs, up to 2 at once, each in its own browser "
                               "window.")


def test_each_slot_is_a_copy_of_the_profile_without_caches_or_locks(pool, capsys):
    seen = {}

    def look(jid, proc):
        slot = next(c[1] for c in spawner.calls if c[0] == jid)
        seen[jid] = sorted(str(p.relative_to(slot)).replace("\\", "/")
                           for p in slot.rglob("*") if p.is_file())
        return _result(jid), ""
    spawner = FakeSpawner(look)
    assert pool.run(spawner, parallel=2, ids={"1", "2"}) == 0
    assert seen["1"] == seen["2"] == ["Default/Network/Cookies", "Local State"]


def test_the_lines_come_in_finish_order_with_summed_totals(pool, capsys):
    delays = {"1": 0.3, "2": 0.0}

    def behave(jid, proc):
        time.sleep(delays[jid])
        return _result(jid, score=5 if jid == "1" else 2,
                       band="May need an answer or two" if jid == "1" else "Queue it",
                       requests=3, usd=0.0125), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert _out(capsys) == [
        "Checking 2 jobs, up to 2 at once, each in its own browser window.",
        "[1/2] Contoso / Analyst: 2/10, Queue it. Jev so far: 3 request(s), $0.0125",
        "[2/2] Fabrikam / Analyst: 5/10, May need an answer or two. "
        "Jev so far: 6 request(s), $0.0250",
    ]


def test_a_page_the_worker_could_not_read_prints_todays_not_checked_line(pool, capsys):
    def behave(jid, proc):
        return _result(jid, "unread", why="the posting did not load (TimeoutError)",
                       requests=0, usd=0.0), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert _unnumbered(_out(capsys)[1:]) == [
        "[k/2] Contoso / Analyst: not checked (the posting did not load (TimeoutError)). "
        "Jev so far: 0 request(s), $0.0000",
        "[k/2] Fabrikam / Analyst: not checked (the posting did not load (TimeoutError)). "
        "Jev so far: 0 request(s), $0.0000",
    ]


def test_a_job_that_left_the_queue_reads_as_today(pool, capsys):
    def behave(jid, proc):
        return _result(jid, "error", why="left the queue while it was checked", requests=0,
                       usd=0.0), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert _unnumbered(_out(capsys)[1:]) == [
        "[k/2] Contoso / Analyst: left the queue while it was checked. "
        "Jev so far: 0 request(s), $0.0000",
        "[k/2] Fabrikam / Analyst: left the queue while it was checked. "
        "Jev so far: 0 request(s), $0.0000",
    ]


def test_jev_down_starts_no_new_job_and_lets_the_running_ones_finish(pool, capsys):
    running = threading.Event()

    def behave(jid, proc):
        if jid == "1":
            assert running.wait(5)
            return _result(jid, "outage", why="Jev is down (ConnectError)"), ""
        running.set()
        time.sleep(0.3)                 # still running when Jev goes down
        return _result(jid), ""
    spawner = FakeSpawner(behave)
    assert pool.run(spawner, parallel=2) == 1
    assert sorted(c[0] for c in spawner.calls) == ["1", "2"]     # 3 to 7 never started
    assert _out(capsys)[1:] == [
        "[1/7] Fabrikam / Analyst: Jev is down (ConnectError); the check stops here",
        "[2/7] Contoso / Analyst: 2/10, Queue it. Jev so far: 2 request(s), $0.0200",
    ]


def test_a_closed_window_stops_the_pool(pool, capsys):
    running = threading.Event()

    def behave(jid, proc):
        if jid == "1":
            assert running.wait(5)
            return _result(jid, "closed", why="the browser window was closed"), ""
        running.set()
        time.sleep(0.3)
        return _result(jid), ""
    spawner = FakeSpawner(behave)
    assert pool.run(spawner, parallel=2) == 1
    assert sorted(c[0] for c in spawner.calls) == ["1", "2"]
    assert _out(capsys)[1] == ("[1/7] Fabrikam / Analyst: the browser window was closed; the "
                               "check stops here")


def test_no_browser_starting_stops_the_pool_and_says_so_once(pool, capsys):
    """Fix round 1: a worker that refuses (its result an error, its exit not 0)
    stops the pool as the one-job check stops: exit 1, the sentence once with
    no not-checked wrapper, no job after the running ones, nothing noted."""
    why = aa.NO_BROWSER.format(why="RuntimeError")
    all_started = threading.Barrier(3, timeout=5)

    def behave(jid, proc):
        all_started.wait()              # the three running workers all meet it
        proc.returncode = 1
        return _result(jid, "error", why=why, requests=0, usd=0.0), ""
    spawner = FakeSpawner(behave)
    assert pool.run(spawner, parallel=3) == 1
    assert len(spawner.calls) == 3                          # 4 to 7 never started
    assert _out(capsys) == [
        "Checking 7 jobs, up to 3 at once, each in its own browser window.", why]
    assert not any((e.get("difficulty") or {}).get("last_failed_why") for e in pool.entries())
    assert list(pool.root.iterdir()) == []


@pytest.mark.parametrize("why", ["job {jid} is not in the queue", "no job to check"])
def test_a_job_gone_from_the_queue_is_no_refusal(pool, capsys, why):
    """run()'s own exit 2 for one job (removed, or the drain took it) is that
    job's line; the pool goes on."""
    def behave(jid, proc):
        if jid == "1":
            proc.returncode = 2
            return _result(jid, "error", why=why.format(jid=jid), requests=0, usd=0.0), ""
        return _result(jid), ""
    spawner = FakeSpawner(behave)
    assert pool.run(spawner, parallel=2, ids={"1", "2", "3"}) == 0
    assert len(spawner.calls) == 3
    assert any(f"Fabrikam / Analyst: not checked ({why.format(jid='1')}). " in x
               for x in _out(capsys))


def test_a_worker_with_no_result_line_is_noted_as_stopped(pool, capsys):
    def behave(jid, proc):
        if jid == "1":
            time.sleep(0.2)
            return "[1/1] half a line\n", (
                "2026-10-01 10:00:00,123 apply_assess INFO job 1: difficulty walk\n"
                "Traceback (most recent call last):\n"
                "KeyboardInterrupt\n"
                "2026-10-01 10:00:01,456 asyncio DEBUG closing\n")
        return _result(jid), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert _out(capsys)[2] == ("[2/2] Fabrikam / Analyst: not checked (the worker stopped: "
                               "KeyboardInterrupt). Jev so far: 1 request(s), $0.0100")
    noted = pool.entries({"1"})[0]["difficulty"]
    assert noted["last_failed_why"] == "the worker stopped: KeyboardInterrupt"


def test_a_worker_with_only_log_lines_says_its_exit_code(pool, capsys):
    def behave(jid, proc):
        if jid == "1":
            proc.returncode = 3
            return "", "2026-10-01 10:00:00,123 apply_assess INFO job 1: walking\n"
        return _result(jid), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert any("Fabrikam / Analyst: not checked (the worker stopped: exit code 3). " in x
               for x in _out(capsys))
    assert pool.entries({"1"})[0]["difficulty"]["last_failed_why"] == (
        "the worker stopped: exit code 3")


def test_a_stopped_worker_on_a_job_that_left_the_queue_is_no_error(pool, capsys):
    def behave(jid, proc):
        apply_queue.remove(jid, path=pool.queue)
        return "", "boom\n"
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert sum("not checked (the worker stopped: boom)" in x for x in _out(capsys)) == 2


def test_a_busy_queue_file_does_not_stop_the_other_jobs(pool, capsys, monkeypatch):
    def busy(*a, **kw):
        raise TimeoutError("the queue lock is held")
    monkeypatch.setattr(apply_queue, "note_difficulty_failure", busy)
    spawner = FakeSpawner(lambda jid, proc: ("", "boom\n"))
    assert pool.run(spawner, parallel=1, ids={"1", "2", "3"}) == 0
    assert sorted(c[0] for c in spawner.calls) == ["1", "2", "3"]
    assert sum("not checked (the worker stopped: boom)" in x for x in _out(capsys)) == 3


def test_a_worker_that_will_not_start_is_noted_and_the_rest_run(pool, capsys):
    calls = []
    inner = FakeSpawner()

    def spawner(job_id, slot, **kw):
        calls.append(job_id)
        if job_id == "1":
            raise OSError("no python")
        return inner(job_id, slot, **kw)
    assert pool.run(spawner, parallel=2, ids={"1", "2", "3"}) == 0
    assert sorted(calls) == ["1", "2", "3"]
    lines = _out(capsys)
    assert any("Fabrikam / Analyst: not checked (the worker did not start (OSError)). " in x
               for x in lines)
    assert sum("/10, Queue it." in x for x in lines) == 2
    assert pool.entries({"1"})[0]["difficulty"]["last_failed_why"] == (
        "the worker did not start (OSError)")
    assert list(pool.root.iterdir()) == []


def test_the_pool_holds_the_real_profile_for_the_whole_run(pool, capsys):
    held = []

    def behave(jid, proc):
        held.append(profile_lock.sentinel_held(pool.profile))
        return _result(jid), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2", "3"}) == 0
    assert held == [True, True, True]
    assert profile_lock.sentinel_held(pool.profile) is False


def test_the_pool_refuses_while_another_browser_holds_the_profile(pool, capsys):
    from test_profile_lock import hold_sentinel
    spawner = FakeSpawner()
    guard = hold_sentinel(pool.profile)
    try:
        assert pool.run(spawner) == 2
    finally:
        guard.release()
    assert spawner.calls == []
    captured = capsys.readouterr()
    assert captured.err.strip() == aa.PROFILE_BUSY and captured.out == ""
    assert not pool.root.exists()


def test_the_slots_are_swept_when_the_run_ends(pool, capsys):
    assert pool.run(FakeSpawner(), parallel=3) == 0
    assert list(pool.root.iterdir()) == []


def test_leftover_slots_are_swept_before_the_run(pool, capsys):
    _write(pool.root, "slot-9/Default/Network/Cookies", "old")
    seen = []

    def behave(jid, proc):
        seen.append(sorted(p.name for p in pool.root.iterdir()))
        return _result(jid), ""
    assert pool.run(FakeSpawner(behave), parallel=2, ids={"1", "2"}) == 0
    assert seen and all(s == ["slot-1", "slot-2"] for s in seen)


def test_an_error_in_one_job_is_its_line_and_the_rest_run(pool, capsys, monkeypatch):
    real = aa.parse_result_line

    def flaky(text):
        if '"job_id":"1"' in text:
            raise RuntimeError("a bug")
        return real(text)
    monkeypatch.setattr(aa, "parse_result_line", flaky)
    assert pool.run(FakeSpawner(), parallel=2, ids={"1", "2", "3"}) == 0
    lines = _out(capsys)
    assert any("Fabrikam / Analyst: not checked (the check failed (RuntimeError)). " in x
               for x in lines)
    assert sum("/10, Queue it." in x for x in lines) == 2
    assert list(pool.root.iterdir()) == []


def test_the_slots_are_swept_and_the_profile_freed_when_the_pool_itself_fails(pool, capsys,
                                                                             monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("a bug in the coordinator")
    monkeypatch.setattr(threading, "Thread", broken)
    with pytest.raises(RuntimeError):
        pool.run(FakeSpawner(), parallel=2, ids={"1", "2"})
    assert list(pool.root.iterdir()) == []
    assert profile_lock.sentinel_held(pool.profile) is False


def test_a_profile_that_will_not_copy_stops_before_any_worker(pool, capsys, monkeypatch):
    real = ap.snapshot_profile

    def no_copy(src, dest):
        if Path(dest).name == "slot-2":
            raise PermissionError("denied")
        return real(src, dest)
    monkeypatch.setattr(ap, "snapshot_profile", no_copy)
    spawner = FakeSpawner()
    assert pool.run(spawner, parallel=3) == 1
    assert spawner.calls == []
    assert _out(capsys)[-1] == ap.NO_COPY.format(why="PermissionError")
    assert list(pool.root.iterdir()) == []
    assert profile_lock.sentinel_held(pool.profile) is False


def test_files_that_will_not_copy_are_logged_and_the_run_goes_on(pool, capsys, monkeypatch,
                                                                caplog):
    real = ap.snapshot_profile
    monkeypatch.setattr(ap, "snapshot_profile",
                        lambda src, dest: real(src, dest) + [str(Path(src) / "Default/x")])
    with caplog.at_level("WARNING", logger="assess_pool"):
        assert pool.run(FakeSpawner(), parallel=2, ids={"1", "2"}) == 0
    assert "slot-1: 1 file(s) of the profile were not copied" in caplog.text


def test_a_missing_profile_is_made_as_today(pool, capsys):
    import shutil
    shutil.rmtree(pool.profile)
    spawner = FakeSpawner()
    assert pool.run(spawner, parallel=2, ids={"1", "2"}) == 0
    assert len(spawner.calls) == 2 and pool.profile.is_dir()


def test_ctrl_c_ends_the_workers_sweeps_and_exits_one(pool, capsys):
    import _thread

    def behave(jid, proc):
        if jid == "1":
            time.sleep(0.1)
            _thread.interrupt_main()
        proc.ended.wait(5)
        return "", "KeyboardInterrupt\n"
    spawner = FakeSpawner(behave)
    assert pool.run(spawner, parallel=2) == 1
    assert sorted(c[0] for c in spawner.calls) == ["1", "2"]
    assert [p.terminated for p in spawner.procs] == [True, True]
    lines = _out(capsys)
    assert lines[-1] == ap.INTERRUPTED
    assert not any("not checked" in x for x in lines)
    assert not any((e.get("difficulty") or {}).get("last_failed_why") for e in pool.entries())
    assert list(pool.root.iterdir()) == []
    assert profile_lock.sentinel_held(pool.profile) is False


def test_the_answers_file_damaged_refuses_as_today(pool, capsys, monkeypatch):
    from resume_tailor import apply_answers

    def damaged(*a, **kw):
        raise apply_answers.AnswerStoreError(Path("answers.json"), "not JSON")
    monkeypatch.setattr(apply_answers, "load", damaged)
    spawner = FakeSpawner()
    assert pool.run(spawner) == 2
    assert spawner.calls == []
    assert _out(capsys) == [f"The Apply Answers file is damaged ({Path('answers.json')}): not "
                            f"JSON. Open the dashboard's Apply Answers tab to restore the "
                            f"backup."]


def test_unknown_ids_are_said_first(pool, capsys):
    assert pool.run(FakeSpawner(), ids={"1", "2"}, unknown=["99"]) == 0
    assert _out(capsys)[:2] == ["apply_assess: job 99 is not in the queue",
                                "Checking 2 jobs, up to 2 at once, each in its own browser "
                                "window."]


def test_the_parallel_setting_is_clamped():
    assert [ap.parallel_setting(v) for v in (0, 1, 3, 10, 11, -5)] == [1, 1, 3, 10, 10, 1]
    assert [ap.parallel_setting(v) for v in (None, "", "x", "4", 2.0, True)] == [
        10, 10, 10, 4, 2, 10]
    assert ap.PARALLEL_MAX == ap.PARALLEL_DEFAULT == 10


def test_the_worker_command_line(tmp_path):
    argv = ap.worker_argv("42", tmp_path / "slot-1", queue_path=tmp_path / "q.json",
                          headless=True)
    assert argv == [sys.executable, str(ap.SCRIPT), "--worker", "--profile",
                    str(tmp_path / "slot-1"), "--queue", str(tmp_path / "q.json"),
                    "--headless", "42"]
    assert ap.worker_argv("7", tmp_path / "slot-2") == [
        sys.executable, str(ap.SCRIPT), "--worker", "--profile", str(tmp_path / "slot-2"), "7"]
    assert ap.SCRIPT.name == "apply_assess.py" and ap.SCRIPT.is_file()


def test_the_default_spawner_captures_the_workers_output(tmp_path, monkeypatch):
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        "print('Fabrikam \\u00e9 ' + ' '.join(sys.argv[1:]))\n"
        "print('a log line', file=sys.stderr)\n", encoding="utf-8")
    monkeypatch.setattr(ap, "SCRIPT", script)
    proc = ap.spawn_worker("42", tmp_path / "slot-1")
    out, err = proc.communicate()
    assert proc.returncode == 0
    assert out.strip() == f"Fabrikam é --worker --profile {tmp_path / 'slot-1'} 42"
    assert err.strip() == "a log line"


# --- apply_assess.main: when the pool runs -------------------------------------------------

@pytest.fixture
def main_env(monkeypatch, tmp_path):
    """`apply_assess.main` past its gates with no browser: the hermetic switch
    on, a fake judge, a queue of three jobs; `run_pool` and `run` record."""
    import apply_run
    import jev
    import jev_switch
    import settings
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    conf = dict(apply_run.DEFAULT_SETTINGS)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(conf))
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)
    monkeypatch.setattr(jev, "get", lambda mode="": jev.FakeJev())

    def _never(*a, **kw):
        raise AssertionError("the real settings store was read")
    monkeypatch.setattr(settings, "load", _never)
    monkeypatch.setattr(settings, "secret_status", _never)
    path = jev_switch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jev_enabled": True, "jev_difficulty": True,
                                "auto_apply_jev_mode": "typesafe"}), encoding="utf-8")
    queue = tmp_path / "queue.json"
    for i in (1, 2, 3):
        apply_queue.enqueue(apply_queue.new_entry(str(i), company=f"C{i}", title="T"),
                            path=queue)
    calls = {"pool": [], "run": []}
    monkeypatch.setattr(ap, "run_pool", lambda chosen, **kw: calls["pool"].append(
        ([e["job_posting_id"] for e in chosen], kw)) or 0)
    monkeypatch.setattr(aa, "run", lambda job_ids, **kw: calls["run"].append(job_ids) or 0)

    class _Env:
        pass
    env = _Env()
    env.conf, env.calls, env.queue = conf, calls, queue
    env.argv = lambda *extra: ["--queue", str(queue), *extra]
    return env


def _fake_browser(monkeypatch):
    pytest.importorskip("playwright")
    import types

    import playwright.sync_api as sync_api

    class _Ctx:
        pages: list = []

        def on(self, *a):
            pass

        def close(self):
            pass

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            return _Ctx()

    class _Starter:
        def __enter__(self):
            return types.SimpleNamespace(chromium=_Chromium())

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Starter())


def test_main_runs_the_pool_for_two_or_more_jobs(main_env):
    assert aa.main(main_env.argv("1", "2", "3")) == 0
    [(ids, kw)] = main_env.calls["pool"]
    assert ids == ["1", "2", "3"] and kw["parallel"] == 10
    assert kw["profile"] == aa.default_profile_dir() and kw["queue_path"] == main_env.queue
    assert kw["unknown"] == [] and kw["headless"] is False
    assert main_env.calls["run"] == []


def test_main_runs_the_pool_for_all_queued_jobs(main_env):
    assert aa.main(main_env.argv("--all", "--headless")) == 0
    [(ids, kw)] = main_env.calls["pool"]
    assert ids == ["1", "2", "3"] and kw["headless"] is True


def test_main_passes_the_unknown_ids_to_the_pool(main_env):
    assert aa.main(main_env.argv("1", "9", "2")) == 0
    [(ids, kw)] = main_env.calls["pool"]
    assert ids == ["1", "2"] and kw["unknown"] == ["9"]


@pytest.mark.parametrize("flag, parallel", [("2", 2), ("50", 10), ("10", 10)])
def test_the_parallel_flag_overrides_the_setting(main_env, flag, parallel):
    main_env.conf["auto_apply_check_parallel"] = 4
    assert aa.main(main_env.argv("--parallel", flag, "1", "2")) == 0
    assert main_env.calls["pool"][0][1]["parallel"] == parallel


def test_main_reads_the_setting(main_env):
    main_env.conf["auto_apply_check_parallel"] = 3
    assert aa.main(main_env.argv("1", "2")) == 0
    assert main_env.calls["pool"][0][1]["parallel"] == 3


@pytest.mark.parametrize("argv, conf, ids", [
    (["1"], {}, ["1"]),                                             # one job
    (["1", "9"], {}, ["1", "9"]),                                   # one known job
    (["--parallel", "1", "1", "2"], {}, ["1", "2"]),                # the flag at 1
    (["--parallel", "0", "1", "2"], {}, ["1", "2"]),                # clamped to 1
    (["1", "2"], {"auto_apply_check_parallel": 1}, ["1", "2"]),     # the setting at 1
])
def test_one_job_or_parallel_one_takes_todays_path(main_env, monkeypatch, argv, conf, ids):
    _fake_browser(monkeypatch)
    main_env.conf.update(conf)
    assert aa.main(main_env.argv(*argv)) == 0
    assert main_env.calls["pool"] == [] and main_env.calls["run"] == [ids]


def test_an_injected_context_never_takes_the_pool(main_env):
    assert aa.main(main_env.argv("1", "2"), context=object()) == 0
    assert main_env.calls["pool"] == [] and main_env.calls["run"] == [["1", "2"]]


def test_a_worker_never_takes_the_pool(main_env, tmp_path, monkeypatch, capsys):
    _fake_browser(monkeypatch)
    assert aa.main(["--worker", "--profile", str(tmp_path / "slot-1"), "--queue",
                    str(main_env.queue), "1"]) == 0
    assert main_env.calls["pool"] == [] and main_env.calls["run"] == [["1"]]


def test_the_pool_is_gated_like_today(main_env, monkeypatch, capsys):
    monkeypatch.setattr(aa, "profile_busy", lambda profile=None: True)
    assert aa.main(main_env.argv("1", "2")) == 2
    assert capsys.readouterr().err.strip() == aa.PROFILE_BUSY
    assert main_env.calls["pool"] == []


def test_the_parallel_default_is_in_the_auto_apply_defaults():
    import apply_run
    assert apply_run.DEFAULT_SETTINGS["auto_apply_check_parallel"] == 10
