"""File-driven headed Playwright REPL — graduated from a proven scratch driver that
ran the live CITGO SuccessFactors account-creation run end-to-end.

ONE long-lived headed browser per ``serve --workdir DIR`` (persistent profile at
``DIR/profile`` so an in-progress signup/session survives across steps — two serves
must NEVER share a profile dir, Chromium locks it). A separate process (the
orchestrator or a subagent) writes ``DIR/cmd.json`` ({seq, action, ...}) via
``send``; this loop executes it and writes ``DIR/out.json`` ({seq, ok, url, title,
inputs, clickables, text, ...}) + ``DIR/shot_<seq>.png``.

SECRET SAFETY: the "paste" action focuses a field and issues a Ctrl+V KEYSTROKE
only — it pastes whatever the OS clipboard holds (loaded out-of-band by
``ats_accounts.py clip-password``) and NEVER reads the clipboard or the field
value. It reports the field's value LENGTH only (``pw_len``, a number), so a paste
can be confirmed without the secret ever entering this process, this file, or any
output.

The "park" action writes ``DIR/parked.json`` ({url, title, ts}) — a machine-
readable signal that the job reached its review page — and does NOT quit: the
browser stays open and the serve loop stays alive so the human can review and
submit. The loop exits 0 cleanly when the human closes the browser/page
(Playwright's "target closed" error, detected on any page operation).

PERSISTENCE: a headed browser dies with its serve process, and an agent's shells
are ephemeral — so the drain must start the driver with ``launch`` (a DETACHED
serve that outlives the subagent AND the orchestrator), never ``serve &`` inside a
subagent (which is reaped the moment the job parks, closing the window before the
human returns). If a parked window is ever gone anyway (crash, reboot, human closed
it), ``reopen`` relaunches the SAME persistent profile at the parked URL, restoring
the logged-in session where the run left off.

UPLOADS: ``set_files`` drives a reachable ``<input type=file>`` (main frame, then any
child frame). ``upload`` intercepts the browser's file chooser instead — the robust
path for iframe/dynamically-created inputs and native OS file dialogs (SuccessFactors).

CLI:
    python local/apply_driver.py launch --workdir DIR [--headless]  # detached serve (survives the agent)
    python local/apply_driver.py serve  --workdir DIR [--headless]  # foreground serve (tests / manual)
    python local/apply_driver.py send   --workdir DIR '{"action":"goto","url":"..."}'
    python local/apply_driver.py reopen --workdir DIR               # restore a parked window
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# Text Playwright uses (across versions) when an operation hits a closed
# page/context/browser. Checked as a substring — version-tolerant, and lets a
# hermetic test raise a plain RuntimeError with this text instead of a real
# Playwright exception type.
_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "target closed",
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_closed_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like Playwright's 'browser/page was closed' error."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _CLOSED_MARKERS)


# ── page introspection ───────────────────────────────────────────────────────

def dump(page, seq, workdir: Optional[Path] = None) -> Dict[str, Any]:
    """Snapshot the page: url/title/visible inputs/clickables/body text excerpt,
    plus a screenshot on disk (when a workdir is given). Every sub-step is
    best-effort — a failing probe records its error string, it never raises."""
    info: Dict[str, Any] = {"seq": seq, "ok": True, "url": page.url, "title": ""}
    try:
        info["title"] = page.title()
    except Exception:  # noqa: BLE001
        pass
    try:
        info["inputs"] = page.eval_on_selector_all(
            "input, textarea, select",
            "els => els.slice(0,60).map(e => ({tag:e.tagName.toLowerCase(),"
            "type:e.type||'', id:e.id||'', name:e.name||'',"
            "ph:e.placeholder||'', aria:e.getAttribute('aria-label')||'',"
            "vis:!!(e.offsetWidth||e.offsetHeight)}))")
    except Exception as e:  # noqa: BLE001
        info["inputs"] = f"err: {e}"
    try:
        info["clickables"] = page.eval_on_selector_all(
            "button, a, [role=button], input[type=submit]",
            "els => els.filter(e=>e.offsetWidth||e.offsetHeight).slice(0,50)"
            ".map(e => ({t:(e.innerText||e.value||e.getAttribute('aria-label')||'')"
            ".trim().slice(0,60), id:e.id||''}))")
    except Exception as e:  # noqa: BLE001
        info["clickables"] = f"err: {e}"
    try:
        info["text"] = page.inner_text("body")[:1800]
    except Exception as e:  # noqa: BLE001
        info["text"] = f"err: {e}"
    if workdir is not None:
        try:
            page.screenshot(path=str(Path(workdir) / f"shot_{seq}.png"), full_page=False)
        except Exception:  # noqa: BLE001
            pass
    return info


# ── action dispatch ──────────────────────────────────────────────────────────

def do(page, cmd: Dict[str, Any], workdir: Optional[Path] = None) -> Dict[str, Any]:
    """Execute one command against ``page``. Ported verbatim (semantics-wise) from
    the proven scratch driver; only the workdir plumbing (for dump/park/set_files)
    and the new ``park`` action are additions."""
    a = cmd.get("action")
    seq = cmd.get("seq")
    if a == "goto":
        page.goto(cmd["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(cmd.get("wait", 3000))
    elif a == "wait":
        page.wait_for_timeout(cmd.get("ms", 2000))
    elif a == "fill":
        page.fill(cmd["selector"], cmd["value"])
    elif a == "fill_label":
        page.get_by_label(cmd["label"], exact=cmd.get("exact", False)).first.fill(cmd["value"])
    elif a == "type":
        page.locator(cmd["selector"]).first.click()
        page.keyboard.type(cmd["value"], delay=40)
    elif a == "click":
        page.locator(cmd["selector"]).first.click(timeout=cmd.get("timeout", 15000))
        page.wait_for_timeout(cmd.get("wait", 2500))
    elif a == "click_text":
        role = cmd.get("role", "button")
        page.get_by_role(role, name=cmd["text"], exact=cmd.get("exact", False)).first.click(
            timeout=cmd.get("timeout", 15000))
        page.wait_for_timeout(cmd.get("wait", 2500))
    elif a == "press":
        page.keyboard.press(cmd["key"])
        page.wait_for_timeout(cmd.get("wait", 1500))
    elif a == "paste":
        # Secret-safe: focus + Ctrl+V keystroke; report LENGTH only, never the value.
        page.locator(cmd["selector"]).first.click()
        page.wait_for_timeout(200)
        page.keyboard.press("Control+V")
        page.wait_for_timeout(400)
        info = dump(page, seq, workdir=workdir)
        try:
            info["pw_len"] = page.eval_on_selector(cmd["selector"], "e => (e.value||'').length")
        except Exception as e:  # noqa: BLE001
            info["pw_len"] = f"err: {e}"
        return info
    elif a == "clear":
        page.locator(cmd["selector"]).first.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    elif a == "set_files":
        # Frame-aware: try the main frame, then any child frame (iframe-embedded
        # upload widgets). For inputs that don't exist until a button is clicked, or
        # that open a native OS dialog, use "upload" instead — it needs no reachable
        # <input type=file> at all.
        _set_files(page, cmd["selector"], cmd["paths"], cmd.get("timeout", 8000))
        page.wait_for_timeout(1500)
    elif a == "upload":
        # Robust upload: intercept the browser's file chooser instead of requiring a
        # reachable <input type=file>. Works for iframe-embedded / dynamically-created
        # inputs AND for "+"/"Upload" buttons that open a native OS file dialog
        # (e.g. SuccessFactors "My Documents") — the exact case set_input_files can't
        # reach. Trigger by CSS ("trigger") or by role+name ("trigger_text").
        _upload_via_chooser(page, cmd)
        page.wait_for_timeout(cmd.get("wait", 1500))
        return dump(page, seq, workdir=workdir)
    elif a == "park":
        # Machine-readable park signal for the one-shot driver + orchestrator:
        # write parked.json and keep the loop (and the browser) alive.
        info = dump(page, seq, workdir=workdir)
        if workdir is not None:
            parked = {"url": page.url, "title": info.get("title", ""), "ts": time.time()}
            Path(workdir).mkdir(parents=True, exist_ok=True)
            (Path(workdir) / "parked.json").write_text(
                json.dumps(parked, ensure_ascii=False), encoding="utf-8")
        info["parked"] = True
        return info
    elif a == "quit":
        return {"seq": seq, "ok": True, "quit": True}
    return dump(page, seq, workdir=workdir)


# ── upload helpers ─────────────────────────────────────────────────────────────

def _child_frames(page) -> List[Any]:
    """Every frame on the page except the main one (best-effort; never raises)."""
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001
        return []
    try:
        main = page.main_frame
    except Exception:  # noqa: BLE001
        main = None
    return [f for f in frames if f is not main]


def _set_files(page, selector: str, paths, timeout: int) -> None:
    """``set_input_files`` on the main frame; on a miss, retry the same selector in
    each child frame (upload widgets are frequently iframe-embedded). If no frame
    has the input, re-raise the main-frame error so do()'s caller reports it — the
    subagent should then fall back to the ``upload`` action."""
    try:
        page.set_input_files(selector, paths, timeout=timeout)
        return
    except Exception as e:  # noqa: BLE001
        if _is_closed_error(e):
            raise
    for frame in _child_frames(page):
        try:
            frame.set_input_files(selector, paths, timeout=timeout)
            return
        except Exception:  # noqa: BLE001
            continue
    # Surface the real failure (main-frame miss) to the report.
    page.set_input_files(selector, paths, timeout=timeout)


def _upload_via_chooser(page, cmd: Dict[str, Any]) -> None:
    """Click the upload trigger and satisfy the resulting file chooser with
    ``cmd['paths']``. Intercepting the chooser works at the CDP layer regardless of
    where (or whether) an ``<input type=file>`` lives in the DOM — iframe, shadow
    DOM, created-on-click, or a native OS dialog."""
    paths = cmd["paths"]
    timeout = cmd.get("timeout", 15000)
    with page.expect_file_chooser(timeout=timeout) as fc_info:
        if cmd.get("trigger_text"):
            page.get_by_role(cmd.get("role", "button"), name=cmd["trigger_text"],
                             exact=cmd.get("exact", False)).first.click(timeout=timeout)
        else:
            page.locator(cmd["trigger"]).first.click(timeout=timeout)
    fc_info.value.set_files(paths)


# ── serve side ────────────────────────────────────────────────────────────────

def _serve_step(page, workdir: Path, last_seq: int, return_exit: bool = False):
    """Process at most one pending command in ``workdir/cmd.json``. Returns the
    new ``last_seq`` (unchanged if there was nothing new to do), or, when
    ``return_exit=True``, a ``(last_seq, should_exit)`` tuple — ``should_exit`` is
    True when the page/browser was closed by the human and the serve loop should
    stop. Factored out of ``serve()`` so tests can drive it with an injected
    FakePage — no real browser anywhere in the test suite."""
    workdir = Path(workdir)
    cmd_path = workdir / "cmd.json"
    out_path = workdir / "out.json"
    should_exit = False
    if cmd_path.exists():
        try:
            cmd = json.loads(cmd_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cmd = None
        if cmd is not None and cmd.get("seq") != last_seq:
            last_seq = cmd.get("seq")
            try:
                cmd_path.unlink()
            except OSError:
                pass
            try:
                res = do(page, cmd, workdir=workdir)
            except Exception as e:  # noqa: BLE001
                if _is_closed_error(e):
                    should_exit = True
                res = {"seq": cmd.get("seq"), "ok": False,
                       "error": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()[-800:]}
                if not should_exit:
                    try:
                        res["url"] = page.url
                        page.screenshot(path=str(workdir / f"shot_{cmd.get('seq')}.png"))
                    except Exception as e2:  # noqa: BLE001
                        if _is_closed_error(e2):
                            should_exit = True
            out_path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            _log(f"seq {cmd.get('seq')} {cmd.get('action')} -> ok={res.get('ok')}")
            if res.get("quit"):
                should_exit = True
    if return_exit:
        return last_seq, should_exit
    return last_seq


def serve(workdir: str, headless: bool = False, page=None) -> int:
    """The long-lived loop. When ``page`` is injected (tests only) no Playwright
    context is launched — the caller drives the fake page directly. In real use
    (``page is None``) this launches ONE headed persistent-profile browser at
    ``workdir/profile`` and polls ``workdir/cmd.json`` until the human closes the
    browser or a ``quit`` command arrives, then exits 0.
    """
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    cmd_path = wd / "cmd.json"
    out_path = wd / "out.json"
    if cmd_path.exists():
        cmd_path.unlink()
    out_path.write_text(json.dumps({"seq": -1, "ok": True, "status": "driver up"}),
                         encoding="utf-8")

    if page is not None:
        # Injected page: caller (tests) owns the browser lifecycle, if any.
        last = -1
        while True:
            last, should_exit = _serve_step(page, wd, last, return_exit=True)
            if should_exit:
                return 0
            time.sleep(0.4)

    from playwright.sync_api import sync_playwright  # lazy: never required at import time

    profile = wd / "profile"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=headless,
            viewport={"width": 1400, "height": 1000})
        live_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _log("driver ready")
        last = -1
        while True:
            try:
                last, should_exit = _serve_step(live_page, wd, last, return_exit=True)
                if should_exit:
                    break
            except Exception as e:  # noqa: BLE001
                if _is_closed_error(e):
                    break
                _log(f"loop err: {e}")
            time.sleep(0.4)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    _log("driver exiting")
    return 0


# ── detached launch / reopen (browser persistence) ─────────────────────────────
#
# WHY: a headed Playwright browser lives only as long as the process that launched
# it (the `with sync_playwright()` block tears it down on exit). The runbook used to
# start `serve` INSIDE the per-job subagent (`serve --workdir … &`); when the subagent
# finished, that background process was reaped and the parked window died — long
# before the human returned. `launch` runs `serve` as a DETACHED process so the window
# outlives the agent; `reopen` restores it from the on-disk persistent profile if it
# ever does close (crash, reboot, human closed it, or a job-object that killed the
# detached child anyway). Detachment is best-effort across OS/job-object policy —
# `reopen` is the guarantee that a parked run is never lost.


def _detach_kwargs(breakaway: bool = True) -> Dict[str, Any]:
    """Popen kwargs to spawn a child that outlives this process as far as the OS
    allows. On Windows, also try to break away from a parent Job object (Claude
    Code may run shells inside one that is kill-on-close); if breakaway is denied
    the caller retries with ``breakaway=False``."""
    if os.name == "nt":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        create_breakaway_from_job = 0x01000000
        flags = detached_process | create_new_process_group
        if breakaway:
            flags |= create_breakaway_from_job
        return {"creationflags": flags}
    return {"start_new_session": True}


def _driver_alive(workdir) -> Optional[int]:
    """The live serve PID recorded in ``workdir/driver.pid``, or None if the file is
    absent / unreadable / points at a dead process. Dependency-free liveness."""
    pid_file = Path(workdir) / "driver.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if ok and code.value != still_active:
                return None
            return pid
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except PermissionError:  # exists, owned by another user
        return pid
    except OSError:
        return None


def _parked_url(workdir) -> Optional[str]:
    """The URL from ``workdir/parked.json`` (where the job parked), or None."""
    p = Path(workdir) / "parked.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("url")
    except Exception:  # noqa: BLE001
        return None


def launch(workdir: str, headless: bool = False) -> int:
    """Spawn a DETACHED ``serve`` process for ``workdir`` and return immediately.

    The parked browser then belongs to an independent OS process, not to the agent
    that called this, so it stays open after the agent's turn ends. The child's
    output goes to ``workdir/serve.log`` (a detached process has no console), and its
    PID is written to ``workdir/driver.pid`` for liveness checks and ``reopen``. If a
    driver is already alive for this workdir, does nothing (relaunching would fight
    the persistent-profile lock)."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    existing = _driver_alive(wd)
    if existing is not None:
        print(f"driver already running pid={existing} workdir={wd}")
        return 0
    args = [sys.executable, str(Path(__file__).resolve()), "serve", "--workdir", str(wd)]
    if headless:
        args.append("--headless")
    logf = open(wd / "serve.log", "ab")  # noqa: SIM115 — inherited by the child
    try:
        try:
            proc = subprocess.Popen(args, stdout=logf, stderr=logf,
                                    stdin=subprocess.DEVNULL, **_detach_kwargs(breakaway=True))
        except OSError:
            # Breakaway (or the flag set) rejected: best-effort detach without it.
            proc = subprocess.Popen(args, stdout=logf, stderr=logf,
                                    stdin=subprocess.DEVNULL, **_detach_kwargs(breakaway=False))
    finally:
        logf.close()
    (wd / "driver.pid").write_text(str(proc.pid), encoding="utf-8")
    print(f"driver launched pid={proc.pid} workdir={wd}")
    return 0


def reopen(workdir: str, headless: bool = False, timeout: float = 90.0) -> int:
    """Restore a parked job's browser. If a detached driver is still alive, report
    where it is and leave it (relaunching would lock-fight the profile). Otherwise
    launch a fresh detached driver on the SAME persistent profile (``workdir/profile``
    — the logged-in session survives on disk) and navigate back to the parked URL from
    ``parked.json``, so the human resumes exactly where the run left off. Returns 0 on
    success, 1 if there's nothing to reopen."""
    wd = Path(workdir)
    alive = _driver_alive(wd)
    if alive is not None:
        print(f"driver already open pid={alive} url={_parked_url(wd) or '(unknown)'} "
              f"workdir={wd}")
        return 0
    if not wd.exists():
        print(f"nothing to reopen: no workdir at {wd}")
        return 1
    launch(str(wd), headless=headless)
    url = _parked_url(wd)
    if url:
        res = send(str(wd), {"action": "goto", "url": url, "wait": 3500}, timeout=timeout)
        print(f"reopened at {url} (ok={res.get('ok')})")
    else:
        print("driver relaunched; no parked.json URL to restore — the profile session is intact")
    return 0


# ── send side ─────────────────────────────────────────────────────────────────

# In-process guard: msvcrt/fcntl byte-0 locks serialize across PROCESSES but a
# same-process second acquire raises "resource deadlock avoided", so pair the
# file lock with a thread lock for the (orchestrator-thread + subagent-thread)
# in-process case too.
_SEQ_THREAD_LOCK = threading.Lock()

# Cross-process seq lock tuning (mirrors apply_queue.locked's non-blocking retry
# — LK_LOCK's own blocking retry raises on contention on Windows).
_SEQ_LOCK_TIMEOUT = 5.0
_SEQ_LOCK_RETRY = 0.01


def _seq_lock_byte0(fh) -> None:
    """One NON-blocking exclusive-lock attempt on byte 0 (raises OSError when held
    elsewhere). msvcrt on Windows, fcntl elsewhere — same split as
    apply_queue.locked / locks.SingleInstance."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _seq_unlock_byte0(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _next_seq(workdir: Path) -> int:
    """Monotonic per-workdir command sequence. The read-modify-write is guarded by
    a thread lock (in-process) AND a sidecar byte-0 file lock (cross-process) so
    two ``send()`` callers sharing a workdir (orchestrator + a subagent, or a
    reopen racing a send) can't both read N and both write N+1, losing a command.
    A hand-edited / corrupt seq.txt no longer kills the caller: an unparseable
    value falls back to 0 (so the next seq is 1)."""
    seq_path = workdir / "seq.txt"
    lock_path = workdir / "seq.txt.lock"
    with _SEQ_THREAD_LOCK:
        fh = open(lock_path, "a+b")  # noqa: SIM115 — closed in finally
        got = False
        try:
            deadline = time.monotonic() + _SEQ_LOCK_TIMEOUT
            while True:
                try:
                    _seq_lock_byte0(fh)
                    got = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break  # give up the cross-process guard; thread lock still holds
                    time.sleep(_SEQ_LOCK_RETRY)
            try:
                prev = (int(seq_path.read_text(encoding="utf-8").strip())
                        if seq_path.exists() else 0)
            except (ValueError, OSError):
                prev = 0  # corrupt/hand-edited seq.txt — recover, don't crash the loop
            seq = prev + 1
            seq_path.write_text(str(seq), encoding="utf-8")
            return seq
        finally:
            if got:
                try:
                    _seq_unlock_byte0(fh)
                except OSError:
                    pass
            fh.close()


def _wait_for_result(workdir: Path, seq: int, timeout: float = 60.0) -> Optional[Dict[str, Any]]:
    out_path = workdir / "out.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out_path.exists():
            try:
                r = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                r = None
            if r and r.get("seq") == seq:
                return r
        time.sleep(0.3)
    return None


def _print_summary(seq: int, res: Dict[str, Any]) -> None:
    print(f"seq={seq} ok={res.get('ok')} url={res.get('url', '')}")
    if res.get("title"):
        print(f"title: {res['title']}")
    if "pw_len" in res:
        print(f"PW FIELD LENGTH: {res['pw_len']}")
    if res.get("parked"):
        print("PARKED - browser left open, loop still running")
    if res.get("error"):
        print(f"ERROR: {res['error']}")
        if res.get("trace"):
            print(res["trace"])
    inp = res.get("inputs")
    if isinstance(inp, list):
        print("--- inputs (visible) ---")
        for e in inp:
            if e.get("vis"):
                tag = e.get("tag")
                bits = [f"{k}={e[k]}" for k in ("type", "id", "name", "ph", "aria") if e.get(k)]
                print(f"  {tag}: " + " ".join(bits))
    elif inp:
        print(f"inputs: {inp}")
    cl = res.get("clickables")
    if isinstance(cl, list):
        texts = [c.get("t") for c in cl if c.get("t")]
        print("--- clickables ---")
        print("  " + " | ".join(texts[:40]))
    txt = res.get("text", "")
    if txt:
        print("--- text excerpt ---")
        print(txt[:1200])
    print(f"(screenshot: shot_{seq}.png)")


def send(workdir: str, cmd: Dict[str, Any], timeout: float = 60.0,
          print_summary: bool = False) -> Dict[str, Any]:
    """Assign the next monotonic seq under ``workdir``, write cmd.json, and wait
    for the matching out.json (up to ``timeout`` seconds). Returns the result
    dict (with ``seq`` always present); on timeout returns
    ``{"seq": seq, "ok": False, "error": "timeout"}``. When ``print_summary`` is
    set, also prints the compact UTF-8-safe summary the CLI prints."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    seq = _next_seq(wd)
    cmd = dict(cmd)
    cmd["seq"] = seq
    (wd / "cmd.json").write_text(json.dumps(cmd), encoding="utf-8")
    res = _wait_for_result(wd, seq, timeout=timeout)
    if res is None:
        res = {"seq": seq, "ok": False, "error": "timeout"}
    if print_summary:
        if res.get("error") == "timeout":
            print(f"TIMEOUT waiting for seq {seq}")
        else:
            _print_summary(seq, res)
    return res


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        prog="apply_driver",
        description="File-driven headed Playwright REPL (serve) + command sender (send).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the long-lived headed browser loop")
    p_serve.add_argument("--workdir", required=True, help="per-run directory (profile/cmd/out)")
    p_serve.add_argument("--headless", action="store_true")

    p_send = sub.add_parser("send", help="send one command and print its result")
    p_send.add_argument("--workdir", required=True, help="the serve process's workdir")
    p_send.add_argument("json_cmd", help='command JSON, e.g. \'{"action":"dump"}\'')

    p_launch = sub.add_parser(
        "launch", help="spawn a DETACHED serve process that outlives this agent")
    p_launch.add_argument("--workdir", required=True, help="per-run directory (profile/cmd/out)")
    p_launch.add_argument("--headless", action="store_true")

    p_reopen = sub.add_parser(
        "reopen", help="restore a parked browser (persistent profile -> parked URL)")
    p_reopen.add_argument("--workdir", required=True, help="the parked job's driver workdir")
    p_reopen.add_argument("--headless", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        return serve(args.workdir, headless=args.headless)

    if args.cmd == "send":
        cmd = json.loads(args.json_cmd)
        res = send(args.workdir, cmd, print_summary=True)
        return 0 if res.get("error") != "timeout" else 1

    if args.cmd == "launch":
        return launch(args.workdir, headless=args.headless)

    if args.cmd == "reopen":
        return reopen(args.workdir, headless=args.headless)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
