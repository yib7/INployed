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

CLI:
    python local/apply_driver.py serve --workdir DIR [--headless]
    python local/apply_driver.py send --workdir DIR '{"action":"goto","url":"..."}'
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

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
        page.set_input_files(cmd["selector"], cmd["paths"])
        page.wait_for_timeout(1500)
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


# ── send side ─────────────────────────────────────────────────────────────────

def _next_seq(workdir: Path) -> int:
    seq_path = workdir / "seq.txt"
    seq = (int(seq_path.read_text().strip()) + 1) if seq_path.exists() else 1
    seq_path.write_text(str(seq))
    return seq


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

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        return serve(args.workdir, headless=args.headless)

    if args.cmd == "send":
        cmd = json.loads(args.json_cmd)
        res = send(args.workdir, cmd, print_summary=True)
        return 0 if res.get("error") != "timeout" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
