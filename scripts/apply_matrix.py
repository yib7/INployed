"""Run every registered auto-apply flow under the fake judge and the noisy
judge's seeds, and print the matrix: per run the flow, the judge, whether it
reached its expected end, the end and its reason, and the invariant breaks;
then the success rate per flow and overall.

    python scripts/apply_matrix.py [--seeds N] [--flows a,b] [--real-timing]
                                   [--json out.json] [--verbose]
                                   [--jobs N] [--flow-timeout S]

The flows, the judges and the invariants are `tests/apply_harness.py`'s. The
run is hermetic: the fixtures are served from `tests/fixtures/` on a local
port, the job folders, queues and account ledgers live in a temp dir, the
master password is a synthetic string, the judge is `FakeJev` or `NoisyJev`
(no key, no network, no spend). Headless Chromium through Playwright.

`--jobs` (default min(8, cpu count)) runs flows in parallel worker
processes, one flow with every judge per worker (`apply_harness.run_matrix`
restricted to that one flow), each with its own `FlowServer` and its own
Chromium. `--jobs 1` keeps the serial path unchanged: one process, one
browser, one server, run through every flow and judge in turn. A worker
that crashes or outlives its flow's timeout (`--flow-timeout`, default 3x an
estimate from the judge count) becomes a "failed" row naming the cause
instead of stalling the batch. The combined rows are put back in the same
order the serial run produces: the registry's flow order, each flow's own
judge order.

Exit 0 when no run broke an invariant, 1 when one did, 2 when the browser
could not start (only checked directly with `--jobs 1`; under `--jobs`
greater than 1 a Chromium that will not launch shows up as a failed row per
flow instead).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import queue
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for sub in ("local", "tests"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

# a flow's serial time across its whole judge list (the brief this option
# was added for: 87 flows x 21 judges, about 53s a flow); the default
# per-flow timeout scales this to the run's own judge count
_BASELINE_FLOW_S = 53.0
_BASELINE_JUDGES = 21          # 1 fake + 20 noisy seeds, the script's own default
_TIMEOUT_FACTOR = 3.0
_MIN_FLOW_TIMEOUT_S = 30.0


def _isolate(tmp: Path, *, appdata: str = "appdata") -> None:
    """Point every per-user store the run could reach at `tmp`, keeping the
    installed Playwright browsers where Playwright looks for them. `appdata`
    lets a parallel worker use its own subdirectory, so two worker processes
    never share one LOCALAPPDATA."""
    real = Path.home() / "AppData" / "Local" / "ms-playwright"
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and real.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(real)
    os.environ["LOCALAPPDATA"] = str(tmp / appdata)
    os.environ["INPLOYED_NO_DOTENV"] = "1"


def _default_flow_timeout(judge_count: int) -> float:
    """`_BASELINE_FLOW_S` scaled to this run's judge count, times
    `_TIMEOUT_FACTOR`: generous enough that a busy machine's real run does
    not trip it, bounded enough that a genuinely hung worker does not stall
    the batch for long. `--flow-timeout` overrides it."""
    return max(_MIN_FLOW_TIMEOUT_S,
              _BASELINE_FLOW_S * (judge_count / _BASELINE_JUDGES) * _TIMEOUT_FACTOR)


# --- the parallel path -----------------------------------------------------------------------

def _run_flow_worker(flow_name: str, seeds: tuple, fast: bool, workdir: str, out_queue) -> None:
    """One flow across every judge in `seeds`, in its own process: this
    worker's own isolation, its own `FlowServer` and its own headless
    Chromium, then `apply_harness.run_matrix` restricted to the one flow, so
    the per-run logic stays the harness's own (the offline guard included:
    `run_matrix` -> `run_flow` calls `apply_harness.offline` on every
    context it opens, worker or not). Streams a ("progress", line) message
    per run, in the same text `main`'s own `--verbose` prints, and ends with
    exactly one ("done", [RunResult, ...]) or ("error", flow_name, cause)."""
    try:
        tmp = Path(workdir)
        _isolate(tmp, appdata=f"appdata-{flow_name}")
        import apply_harness as h
        f = h.flow(flow_name)
        judge_list = h.judges(seeds)
        server = h.FlowServer()
        server.start()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True)
                except Exception as e:      # noqa: BLE001  (Playwright raises its own Error)
                    out_queue.put(("error", flow_name, f"Chromium did not start: {e}"))
                    return
                try:
                    def _progress(r):
                        out_queue.put(("progress", f"{r.flow} {r.judge}: "
                                       f"{'ok' if r.ok else 'NO'} {r.status} ({r.reason}) "
                                       f"{r.seconds}s"))
                    results = h.run_matrix((f,), judge_list, browser=browser, server=server,
                                           workdir=tmp, fast=fast, progress=_progress)
                finally:
                    browser.close()
        finally:
            server.stop()
        out_queue.put(("done", results))
    except Exception:       # noqa: BLE001  (reported as a failure row, never raised)
        out_queue.put(("error", flow_name, traceback.format_exc()))


def _crash_result(flow_name: str, reason: str):
    import apply_harness as h
    return h.RunResult(flow_name, "worker", "failed", reason, False, [], 0, 0, 0.0)


def _next_message(out_q, p, deadline: float):
    """The next queued message, or None once `deadline` passes or the
    worker died with nothing left to read. A dead worker gets one more short
    read first, for whatever its feeder thread flushed on the way out."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return out_q.get(timeout=min(0.2, remaining))
        except queue.Empty:
            if p.is_alive():
                continue
            try:
                return out_q.get(timeout=0.5)
            except queue.Empty:
                return None


def _run_one_flow(name: str, seeds: tuple, fast: bool, workdir: Path, timeout: float,
                  verbose: bool, ctx) -> list:
    """`name` in its own worker process, bounded by `timeout`: the worker's
    own results, or one "failed" row naming a crash or a timeout."""
    out_q = ctx.Queue()
    p = ctx.Process(target=_run_flow_worker, args=(name, seeds, fast, str(workdir), out_q),
                    daemon=True)
    p.start()
    deadline = time.monotonic() + timeout
    payload = None
    while True:
        msg = _next_message(out_q, p, deadline)
        if msg is None:
            break
        if msg[0] == "progress":
            if verbose:
                print(msg[1], flush=True)
            continue
        payload = msg
        break
    p.join(2.0)
    if p.is_alive():
        p.terminate()
        p.join(5.0)
    out_q.close()
    if payload is None:
        if p.exitcode not in (0, None):
            reason = (f"apply_matrix: {name}'s worker exited (code {p.exitcode}) without a "
                      "result")
        else:
            reason = f"apply_matrix: {name} did not finish within {timeout:.0f}s"
        return [_crash_result(name, reason)]
    if payload[0] == "done":
        return payload[1]
    cause = payload[2] if len(payload) > 2 else ""
    cause = cause.strip().splitlines()[-1] if cause.strip() else "unknown error"
    return [_crash_result(name, f"apply_matrix: {name}'s worker failed: {cause}")]


def _run_parallel(flows, seeds: tuple, fast: bool, jobs: int, flow_timeout: float,
                  workdir: Path, *, verbose: bool = False) -> list:
    """Every flow in `flows`, one worker process per flow, `jobs` running at
    once; the combined rows put back in `flows`' own order (each flow's own
    rows already come back in judge order from `run_matrix`), so the output
    matches the serial run's regardless of which worker finishes first."""
    ctx = mp.get_context("spawn")
    names = [f.name for f in flows]
    per_flow: dict[str, list] = {}

    def _task(name: str) -> None:
        per_flow[name] = _run_one_flow(name, seeds, fast, workdir, flow_timeout, verbose, ctx)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        list(ex.map(_task, names))
    results = []
    for name in names:
        results.extend(per_flow[name])
    return results


# --- entry point -------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apply_matrix", description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=20, help="noisy seeds (default 20)")
    ap.add_argument("--flows", default="", help="comma-separated flow names (default all)")
    ap.add_argument("--real-timing", action="store_true",
                    help="keep the production settle and click timeouts")
    ap.add_argument("--json", default="", help="write every run's result here")
    ap.add_argument("--verbose", action="store_true", help="one line per run as it ends")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                    help="parallel worker processes, one flow (every judge) per worker "
                         "(default min(8, cpu count)); 1 keeps the serial path")
    ap.add_argument("--flow-timeout", type=float, default=0.0,
                    help="seconds before a flow's worker is treated as hung and reported as "
                         "a failure row (default: 3x an estimate from the judge count)")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="apply-matrix-") as tmp:
        tmp_path = Path(tmp)
        _isolate(tmp_path)
        import apply_harness as h
        flows = h.FLOWS
        if args.flows:
            wanted = {n.strip() for n in args.flows.split(",") if n.strip()}
            flows = tuple(f for f in h.FLOWS if f.name in wanted)
        noisy_seeds = h.NOISY_SEEDS[:max(0, args.seeds)]
        judge_list = h.judges(noisy_seeds)
        jobs = max(1, args.jobs)
        started = time.monotonic()
        if jobs == 1:
            server = h.FlowServer()
            server.start()
            from playwright.sync_api import sync_playwright
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
                                           workdir=tmp_path, fast=not args.real_timing,
                                           progress=progress)
                    browser.close()
            finally:
                server.stop()
        else:
            timeout = args.flow_timeout if args.flow_timeout > 0 else \
                _default_flow_timeout(len(judge_list))
            results = _run_parallel(flows, noisy_seeds, not args.real_timing, jobs, timeout,
                                    tmp_path, verbose=args.verbose)
        print(h.summary(results))
        rt = h.rates(results)
        print(f"the suite's pinned floors (fake and seeds {h.SUITE_SEEDS[0]} to "
              f"{h.SUITE_SEEDS[-1]}): noisy {h.SUCCESS_FLOOR:.1%}, fake "
              f"{h.FAKE_SUCCESS_FLOOR:.1%}; {len(flows)} flows x {len(judge_list)} judges in "
              f"{time.monotonic() - started:.0f}s")
        if args.json:
            rows = [{k: v for k, v in asdict(r).items() if k != "actions"} for r in results]
            Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return 1 if rt["breaks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
