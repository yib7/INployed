"""Playwright modern-board driver — the resolved upload path for the auto-apply flow.

SP1's capability spike (`docs/auto_apply_capabilities.md`) failed the whole flow on
ONE wall: `mcp__claude-in-chrome__file_upload` only accepts session-shared files, so a
tailored résumé PDF on disk could never be attached. This module is the fix. Playwright's
``page.set_input_files`` drives the file input at the CDP layer (``DOM.setFileInputFiles``),
BELOW the extension's session-share policy AND the page's Content-Security-Policy — so a
plain local path uploads. Proven live on Greenhouse (Gotion, 2026-07-06).

Scope: the standardized Greenhouse-family embed (`job-boards.greenhouse.io` /
`boards.greenhouse.io`, and companies embedding the same form) — stable field ids
(`#first_name`, `#last_name`, `#email`, `#phone`, `#resume`, `#cover_letter`) plus
label-addressed LinkedIn/Website/Country. It fills the identity block it can SOURCE from
apply.md and attaches both files; it does NOT guess answers to custom/EEO questions
(safety invariant 2 — never invent facts), it reports them for the human instead.

**Park is the default and the product.** ``run(..., submit=False)`` fills, uploads, and
STOPS before the Submit button, leaving the tab open for the human to review + submit —
exactly the skill's park-at-review contract. ``submit=True`` is the explicitly-authorized
end-to-end path: it clicks Submit, handles Greenhouse's emailed security-code gate via
``apply_verify`` (the file handshake with the orchestrator's inbox reader), resubmits, and
reports the confirmation. Nothing here submits unless the CALLER passes ``submit=True``.

CLI:
    python local/apply_playwright.py --url <greenhouse-url> --folder <job-folder>   # park (foreground)
    python local/apply_playwright.py --url ... --folder ... --detach                # park, survives the agent
    python local/apply_playwright.py --url ... --folder ... --submit --run-dir <dir> # authorized e2e
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_driver  # noqa: E402  (same local/ dir; reuse _detach_kwargs — no heavy imports)
import apply_verify  # noqa: E402  (same local/ dir; pure stdlib, safe at import time)

# `- **Label:** value` — the shape every Candidate/Address/Standard-answer line uses.
_KV_RE = re.compile(r"^\s*-\s+\*\*(?P<label>[^*]+?):?\*\*\s*(?P<value>.*?)\s*$")
# A `## ` / `### ` section heading; text after `#`s, trailing space tolerated.
_HEADING_RE = re.compile(r"^\s*#{2,3}\s+(?P<name>.+?)\s*$")


def split_name(full: str) -> Tuple[str, str]:
    """Split a full name into (first, last). One token → ("Name", ""); three+ →
    first token is first name, everything after is the last name (keeps
    multi-word surnames intact). Empty in → ("", "")."""
    parts = str(full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_apply_md(text: str) -> Dict[str, Any]:
    """Parse an apply.md into the field values a form fill needs.

    Returns {"candidate": {label: value}, "address": {label: value},
    "standard_answers": [(question, answer)], "signature_name": str}. Labels are
    lowercased with the trailing colon stripped ("First" not "**First:**"); the
    `## Standard answers` questions keep their original text (they ARE the form
    question). Tolerant of hand edits and missing sections — absent → empty.
    """
    candidate: Dict[str, str] = {}
    address: Dict[str, str] = {}
    standard: List[Tuple[str, str]] = []
    signature_name = ""
    section = ""
    for line in str(text or "").splitlines():
        h = _HEADING_RE.match(line)
        if h:
            section = h.group("name").strip().lower()
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        label = m.group("label").strip()
        value = m.group("value").strip()
        low = label.lower()
        if section == "candidate":
            candidate[low] = value
        elif section == "address":
            address[low] = value
        elif section == "standard answers":
            # The whole bold span is the question; the value is the answer.
            standard.append((label.strip(), value))
        elif section.startswith("electronic signature"):
            if low.startswith("signature"):
                signature_name = value
    return {"candidate": candidate, "address": address,
            "standard_answers": standard, "signature_name": signature_name}


def _load_folder(folder: Path) -> Dict[str, Any]:
    """apply.md parse + the résumé/cover PDF paths discovered in the job folder."""
    folder = Path(folder)
    apply_md = folder / "apply.md"
    parsed = parse_apply_md(apply_md.read_text(encoding="utf-8")) if apply_md.exists() else \
        {"candidate": {}, "address": {}, "standard_answers": [], "signature_name": ""}
    pdfs = sorted(folder.glob("*.pdf"))
    resume = next((p for p in pdfs if "resume" in p.name.lower()), None)
    cover = next((p for p in pdfs if "cover" in p.name.lower()), None)
    parsed["resume"] = str(resume) if resume else ""
    parsed["cover"] = str(cover) if cover else ""
    return parsed


# ── Playwright driving (validated in live runs, not unit-tested — needs a browser) ──

def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _fill_if_present(page, selector: str, value: str) -> bool:
    """Fill a field only if it exists and we have a value. Returns whether it filled."""
    if not value:
        return False
    try:
        el = page.query_selector(selector)
        if el:
            el.fill(value)
            return True
    except Exception as e:  # noqa: BLE001
        _log(f"fill {selector}: {e}")
    return False


def _fill_by_label(page, label: str, value: str) -> bool:
    if not value:
        return False
    try:
        loc = page.get_by_label(label, exact=False)
        if loc.count():
            loc.first.fill(value)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def fill_identity(page, parsed: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Fill the standard Greenhouse identity block from parsed apply.md, recording
    each field it set in ``report['filled']``. Only fills fields it can source."""
    cand = parsed.get("candidate", {})
    first, last = split_name(cand.get("name", ""))
    for sel, val, key in (
        ("#first_name", first, "first_name"),
        ("#last_name", last, "last_name"),
        ("#email", cand.get("email", ""), "email"),
        ("#phone", cand.get("phone", ""), "phone"),
    ):
        if _fill_if_present(page, sel, val):
            report["filled"][key] = val
    for label, val, key in (
        ("LinkedIn Profile", cand.get("linkedin", ""), "linkedin"),
        ("Website", cand.get("github / portfolio", "") or cand.get("github", ""), "website"),
    ):
        if _fill_by_label(page, label, val):
            report["filled"][key] = val
    country = parsed.get("address", {}).get("country", "")
    if country:
        try:
            c = page.get_by_label("Country", exact=False)
            if c.count():
                c.first.click()
                page.keyboard.type(country)
                page.wait_for_timeout(700)
                page.keyboard.press("Enter")
                report["filled"]["country"] = country
        except Exception:  # noqa: BLE001
            pass


def upload_files(page, resume: str, cover: str, report: Dict[str, Any]) -> None:
    """Attach résumé + cover via ``set_input_files`` — THE capability this module exists
    for. Records success/failure per file in ``report['uploads']``."""
    for sel, path, key in (("#resume", resume, "resume"),
                           ("#cover_letter", cover, "cover_letter")):
        if not path:
            continue
        try:
            page.set_input_files(sel, path)
            page.wait_for_timeout(1000)
            report["uploads"][key] = True
            _log(f"attached {key}: {Path(path).name}")
        except Exception as e:  # noqa: BLE001
            report["uploads"][key] = False
            _log(f"upload {key} failed: {e}")


def _handle_code_gate(page, run_dir: Path, meta: Dict[str, Any]) -> Optional[str]:
    """Submit-mode only: detect Greenhouse's emailed security-code gate, ask the
    orchestrator for the code via the file handshake, and fill it. Returns the code
    filled, or None if no gate appeared / it timed out."""
    gate = None
    for _ in range(15):
        gate = apply_verify.detect_code_gate(page)
        if gate:
            break
        page.wait_for_timeout(1000)
    if not gate:
        return None
    _log("CODE GATE DETECTED — requesting code from orchestrator")
    apply_verify.request_code(run_dir, meta)
    code = apply_verify.await_code(run_dir, timeout=300, poll=2)
    if not code:
        _log("TIMED OUT waiting for security code")
        return None
    apply_verify.fill_code(page, gate, code)
    page.wait_for_timeout(800)
    return code


def run(url: str, folder: str, *, submit: bool = False, run_dir: Optional[str] = None,
        hold: bool = True, headless: bool = False) -> Dict[str, Any]:
    """Fill a Greenhouse-family application and PARK (default) or, when ``submit=True``,
    complete the authorized end-to-end submit (with security-code handling).

    Returns a structured report. In park mode the browser is left open (``hold``) at the
    filled page — Submit UNCLICKED — for the human to review and submit.
    """
    from playwright.sync_api import sync_playwright  # lazy: pure-parse callers need no browser

    parsed = _load_folder(Path(folder))
    report: Dict[str, Any] = {
        "url": url, "mode": "submit" if submit else "park",
        "filled": {}, "uploads": {}, "code_gate": None,
        "status": "", "confirmation": "",
    }
    rd = Path(run_dir) if run_dir else (Path(folder) / ".apply_run")
    rd.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            fill_identity(page, parsed, report)
            upload_files(page, parsed.get("resume", ""), parsed.get("cover", ""), report)
            _log(f"FILLED identity={list(report['filled'])} uploads={report['uploads']}")
        except Exception as exc:  # noqa: BLE001
            # A launch / goto-timeout / fill / upload crash BEFORE any terminal
            # branch would otherwise propagate with no report.json — leaving the
            # claimed queue entry with no forensic signal. Honour _write_report's
            # "called at EVERY terminal moment" contract: record the failure, hold
            # the window if one opened, then re-raise so the CLI still exits nonzero.
            report["status"] = f"failed: {exc}"
            _log(report["status"])
            _write_report(rd, report)
            if hold and browser is not None:
                _hold(browser)
            raise

        if not submit:
            report["status"] = "PARKED at review - Submit NOT clicked, tab left open"
            _log(report["status"])
            _write_report(rd, report)
            if hold:
                _hold(browser)
            return report

        # ── authorized end-to-end submit path ────────────────────────────────────
        try:
            page.get_by_role("button", name="Submit application",
                             exact=False).first.click()
            _log("SUBMIT CLICKED")
        except Exception as e:  # noqa: BLE001
            report["status"] = f"submit click failed: {e}"
            _write_report(rd, report)
            if hold:
                _hold(browser)
            return report
        # Submit has been CLICKED. From here on, a crash (code-gate handling,
        # resubmit, or reading the confirmation body) must NOT propagate without a
        # report — the application may already be in, and an orchestrator that reads
        # no terminal signal could wrongly re-queue a live submission. Record an
        # unconfirmed-submit report, then re-raise so the CLI still exits nonzero.
        try:
            page.wait_for_timeout(4000)

            code = _handle_code_gate(page, rd, {"url": url, "folder": folder})
            report["code_gate"] = bool(code)
            if apply_verify.detect_code_gate(page) is not None and not code:
                report["status"] = "code gate appeared but no code arrived — parked"
                _write_report(rd, report)
                if hold:
                    _hold(browser)
                return report
            if code:
                for name in ("Resubmit application", "Submit application",
                             "Resubmit", "Submit"):
                    try:
                        b = page.get_by_role("button", name=name, exact=False)
                        if b.count() and b.first.is_visible():
                            b.first.click()
                            _log(f"RESUBMIT CLICKED ({name})")
                            break
                    except Exception:  # noqa: BLE001
                        pass
                page.wait_for_timeout(5000)

            body = page.inner_text("body")
            report["confirmation"] = body[:300].replace("\n", " ")
            report["status"] = "submitted" if "received" in body.lower() \
                or "thank you for applying" in body.lower() else "submitted (unconfirmed)"
            _log(f"POST-SUBMIT: {report['status']}")
            _write_report(rd, report)
            if hold:
                _hold(browser)
        except Exception as exc:  # noqa: BLE001
            report["status"] = f"submitted (unconfirmed — post-submit crash: {exc})"
            _log(report["status"])
            _write_report(rd, report)
            if hold and browser is not None:
                _hold(browser)
            raise
    return report


def _write_report(rd: Path, report: Dict[str, Any]) -> Path:
    """Write ``report`` to ``rd/report.json`` — the one-shot driver's machine-readable
    signal. Called at EVERY terminal moment (park, code-gate park, submit-click
    failure, post-submit) BEFORE any hold, so a watching orchestrator (or the
    ``apply_driver`` ``park`` action's sibling signal) can read the outcome even
    while this process is still holding the browser window open."""
    rd = Path(rd)
    rd.mkdir(parents=True, exist_ok=True)
    report_path = rd / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report_path


def _hold(browser) -> None:
    _log("HOLDING window open - close it to end.")
    try:
        while browser.is_connected():
            time.sleep(2)
    except Exception:  # noqa: BLE001
        pass


def _spawn_detached(args) -> int:
    """Re-run this one-shot as a DETACHED process so the parked browser outlives the
    (ephemeral) agent that started it — the same fix apply_driver.launch applies to the
    serve driver. Without this, backgrounding the one-shot inside a per-job subagent
    (``… &``) means the window dies when the subagent is reaped, before the human
    returns. NOTE: a Greenhouse fill has no server-side draft, so a lost window can only
    be re-queued (no ``reopen`` recovery) — which is exactly why keeping it alive matters.
    Child output → ``<run_dir>/serve.log``; pid → ``<run_dir>/driver.pid``."""
    rd = Path(args.run_dir) if args.run_dir else (Path(args.folder) / ".apply_run")
    rd.mkdir(parents=True, exist_ok=True)
    child = [sys.executable, str(Path(__file__).resolve()),
             "--url", args.url, "--folder", args.folder]  # NB: no --detach → child runs the browser
    if args.submit:
        child.append("--submit")
    if args.run_dir:
        child += ["--run-dir", args.run_dir]
    if args.headless:
        child.append("--headless")
    if args.no_hold:
        child.append("--no-hold")
    logf = open(rd / "serve.log", "ab")  # noqa: SIM115 — inherited by the child
    try:
        try:
            proc = subprocess.Popen(child, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                    **apply_driver._detach_kwargs(breakaway=True))
        except OSError:
            proc = subprocess.Popen(child, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                    **apply_driver._detach_kwargs(breakaway=False))
    finally:
        logf.close()
    (rd / "driver.pid").write_text(str(proc.pid), encoding="utf-8")
    print(f"apply_playwright launched detached pid={proc.pid} run_dir={rd}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="apply_playwright",
        description="Playwright Greenhouse-family driver — parks at review by default.")
    ap.add_argument("--url", required=True, help="the Greenhouse application URL")
    ap.add_argument("--folder", required=True,
                    help="job folder containing apply.md + résumé/cover PDFs")
    ap.add_argument("--submit", action="store_true",
                    help="AUTHORIZED end-to-end submit (default: park at review, never submit)")
    ap.add_argument("--run-dir", default=None,
                    help="handshake dir for the security-code exchange (submit mode)")
    ap.add_argument("--no-hold", action="store_true",
                    help="don't keep the browser open after finishing")
    ap.add_argument("--detach", action="store_true",
                    help="run detached so the parked window outlives the calling agent "
                         "(spawns a child, writes driver.pid, returns immediately)")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)
    if args.detach:
        return _spawn_detached(args)
    report = run(args.url, args.folder, submit=args.submit, run_dir=args.run_dir,
                 hold=not args.no_hold, headless=args.headless)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
