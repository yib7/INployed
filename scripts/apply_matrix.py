"""Run every registered auto-apply flow under the fake judge and the noisy
judge's seeds, and print the matrix: per run the flow, the judge, whether it
reached its expected end, the end and its reason, and the invariant breaks;
then the success rate per flow and overall.

    python scripts/apply_matrix.py [--seeds N] [--flows a,b] [--real-timing]
                                   [--json out.json] [--verbose]

The flows, the judges and the invariants are `tests/apply_harness.py`'s. The
run is hermetic: the fixtures are served from `tests/fixtures/` on a local
port, the job folders, queues and account ledgers live in a temp dir, the
master password is a synthetic string, the judge is `FakeJev` or `NoisyJev`
(no key, no network, no spend). Headless Chromium through Playwright.

Exit 0 when no run broke an invariant, 1 when one did, 2 when the browser
could not start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for sub in ("local", "tests"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))


def _isolate(tmp: Path) -> None:
    """Point every per-user store the run could reach at `tmp`, keeping the
    installed Playwright browsers where Playwright looks for them."""
    real = Path.home() / "AppData" / "Local" / "ms-playwright"
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and real.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(real)
    os.environ["LOCALAPPDATA"] = str(tmp / "appdata")
    os.environ["INPLOYED_NO_DOTENV"] = "1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apply_matrix", description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=20, help="noisy seeds (default 20)")
    ap.add_argument("--flows", default="", help="comma-separated flow names (default all)")
    ap.add_argument("--real-timing", action="store_true",
                    help="keep the production settle and click timeouts")
    ap.add_argument("--json", default="", help="write every run's result here")
    ap.add_argument("--verbose", action="store_true", help="one line per run as it ends")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="apply-matrix-") as tmp:
        _isolate(Path(tmp))
        import apply_harness as h
        flows = h.FLOWS
        if args.flows:
            wanted = {n.strip() for n in args.flows.split(",") if n.strip()}
            flows = tuple(f for f in h.FLOWS if f.name in wanted)
        judge_list = h.judges(h.NOISY_SEEDS[:max(0, args.seeds)])
        server = h.FlowServer()
        server.start()
        from playwright.sync_api import sync_playwright
        started = time.monotonic()
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True)
                except Exception as e:      # noqa: BLE001  (Playwright raises its own Error)
                    print(f"apply_matrix: Chromium did not start: {e}", file=sys.stderr)
                    return 2

                def progress(r):
                    if args.verbose:
                        print(f"{r.flow} {r.judge}: {'ok' if r.ok else 'NO'} {r.status} "
                              f"({r.reason}) {r.seconds}s", flush=True)
                results = h.run_matrix(flows, judge_list, browser=browser, server=server,
                                       workdir=Path(tmp), fast=not args.real_timing,
                                       progress=progress)
                browser.close()
        finally:
            server.stop()
        print(h.summary(results))
        rt = h.rates(results)
        print(f"the suite's pinned floors (fake and seeds {h.SUITE_SEEDS[0]} to "
              f"{h.SUITE_SEEDS[-1]}): noisy {h.SUCCESS_FLOOR:.1%}, fake "
              f"{h.FAKE_SUCCESS_FLOOR:.1%}; {len(flows)} flows x {len(judge_list)} judges in "
              f"{time.monotonic() - started:.0f}s")
        if args.json:
            Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2),
                                       encoding="utf-8")
        return 1 if rt["breaks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
