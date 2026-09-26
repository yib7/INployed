"""Run every registered auto-apply flow under the fake judge and the noisy
judge's seeds, and print the matrix: per run the flow, the judge, whether it
reached its expected end, the end and its reason, and the invariant breaks;
then the success rate per flow and overall.

    python scripts/apply_matrix.py [--seeds N] [--flows a,b] [--real-timing]
                                   [--json out.json] [--verbose]
                                   [--jobs N] [--flow-timeout S]
                                   [--real record|replay|dry] [--real-cache PATH]

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
that crashes or outlives its flow's timeout (`--flow-timeout`, default 2x the
slowest flow's time for the judge count) becomes a "failed" row per judge of its flow,
each naming the cause, instead of stalling the batch: every run the flow
owed counts as a miss (SP6 review R2-M3). The combined rows are put back in
the same order the serial run produces: the registry's flow order, each
flow's own judge order.

`--real` adds the real judge's column (SP8b): one run per flow under the
live model's answers, kept in their own cache (`--real-cache`, default
`tests/fixtures/jev_cache/matrix_cache.json`). `--real replay` runs it
beside the fake and noisy runs, from the cache alone (a miss ends that run
as failed and is counted). `--real record` asks the live model on a miss
and must go through `scripts/jev_record.ps1 -Target matrix`, which holds
the key for that one process; it runs the real column alone, serially, and
stops starting flows at `AUTO_APPLY_RECORD_USD_CAP`. `--real dry` is the
same run with the fake answering at each request's estimated size into a
temp copy of the cache: the request count and the spend a recording would
make, with no key.

Exit 0 when no run broke an invariant and every worker finished, 1 when a
run broke one or a worker crashed or hung, 2 when the browser could not
start (only checked directly with `--jobs 1`; under `--jobs` greater than 1
a Chromium that will not launch shows up as failed rows per flow instead,
and exits 1).
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

# the slowest flow's serial time across its whole judge list: slow_signup's
# page waits 8s after the sign-up, so each of its runs takes about 9.5s and
# its 21 judges about 200s (most flows take about 53s); the default per-flow
# timeout scales this to the run's own judge count
_BASELINE_FLOW_S = 200.0
_BASELINE_JUDGES = 21          # 1 fake + 20 noisy seeds, the script's own default
_TIMEOUT_FACTOR = 2.0
# a floor for a run of few judges: the slowest flow (slow_signup, about 21s
# alone) with a slow Chromium start on a busy machine (final review C N5)
_MIN_FLOW_TIMEOUT_S = 120.0


def _isolate(tmp: Path, *, appdata: str = "appdata") -> None:
    """Point every per-user store the run could reach at `tmp`, keeping the
    installed Playwright browsers where Playwright looks for them, and turn
    off every `.env` load: `resume_tailor.config` loads the repo's `.env`
    when it is imported (the runner's sheet refresh imports it), and it does
    not read INPLOYED_NO_DOTENV. `appdata` lets a parallel worker use its own
    subdirectory, so two worker processes never share one LOCALAPPDATA."""
    real = Path.home() / "AppData" / "Local" / "ms-playwright"
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and real.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(real)
    os.environ["LOCALAPPDATA"] = str(tmp / appdata)
    os.environ["INPLOYED_NO_DOTENV"] = "1"
    try:
        import dotenv
        import dotenv.main
    except ImportError:         # python-dotenv absent: nothing loads a .env
        return
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.main.load_dotenv = lambda *a, **k: False


def _default_flow_timeout(judge_count: int) -> float:
    """`_BASELINE_FLOW_S` scaled to this run's judge count, times
    `_TIMEOUT_FACTOR`: generous enough that a busy machine's real run does
    not trip it, bounded enough that a genuinely hung worker does not stall
    the batch for long. `--flow-timeout` overrides it."""
    return max(_MIN_FLOW_TIMEOUT_S,
              _BASELINE_FLOW_S * (judge_count / _BASELINE_JUDGES) * _TIMEOUT_FACTOR)


# --- the parallel path -----------------------------------------------------------------------

def _run_flow_worker(flow_name: str, seeds: tuple, fast: bool, workdir: str, out_queue,
                     real_cache: str = "") -> None:
    """One flow across every judge in `seeds`, in its own process: this
    worker's own isolation, its own `FlowServer` and its own headless
    Chromium, then `apply_harness.run_matrix` restricted to the one flow, so
    the per-run logic stays the harness's own (the offline guard included:
    `run_matrix` -> `run_flow` calls `apply_harness.offline` on every
    context it opens, worker or not). Streams a ("progress", line) message
    per run, in the same text `main`'s own `--verbose` prints, and ends with
    exactly one ("done", [RunResult, ...]) or ("error", flow_name, cause).
    With `real_cache`, the real judge's replay over that cache runs last
    (`apply_harness.real_judge("replay", ...)`: read-only, so workers may
    share the file)."""
    try:
        tmp = Path(workdir)
        _isolate(tmp, appdata=f"appdata-{flow_name}")
        import apply_harness as h
        f = h.flow(flow_name)
        real = h.real_judge("replay", Path(real_cache)).judge if real_cache else None
        judge_list = h.judges(seeds, real=real)
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


_CRASH_MARK = "apply_matrix: "      # the reason's start on a crashed or hung worker's rows


def _crash_results(flow_name: str, seeds: tuple, reason: str, real: bool = False) -> list:
    """One "failed" row per judge the flow owed (`apply_harness.judges`, the
    real judge's too with `real`), each naming the cause: a crashed or hung
    worker counts every run as a miss."""
    import apply_harness as h
    f = next((f for f in h.FLOWS if f.name == flow_name), None)
    real = real and (f is None or (f.replayable and f.recorded))
    return [h.RunResult(flow_name, judge, "failed", reason, False, [], 0, 0, 0.0)
            for judge, _ in h.judges(seeds, real=object() if real else None)]


def _crashed(results) -> int:
    """How many rows stand for a crashed or hung worker's runs."""
    return sum(1 for r in results if r.status == "failed" and r.reason.startswith(_CRASH_MARK))


def _failures(h, results, *, whole: bool) -> list[str]:
    """What fails the run (final review C-I2): an invariant break, a crashed
    or hung worker, a park outside the user's policy that missed its flow's
    end, the fake judge under `FAKE_SUCCESS_FLOOR`, on a run of the whole
    registry (`whole`) the noisy seeds under `SUCCESS_FLOOR`, and a request
    the real column's replay did not find in its cache (final review C N4);
    [] when the run passes."""
    rt = h.rates(results)
    out = []
    if rt["breaks"]:
        out.append(f"{rt['breaks']} invariant break(s)")
    misses = sum(r.replay_misses for r in results if r.judge == h.REAL)
    if misses:
        out.append(f"{misses} replay miss(es) in the real column")
    crashed = _crashed(results)
    if crashed:
        out.append(f"{crashed} run(s) lost to a crashed or hung worker")
    if rt["outside_policy"]:
        out.append(f"{rt['outside_policy']} park(s) outside the policy")
    if any(r.judge == "fake" for r in results) and rt["fake"] < h.FAKE_SUCCESS_FLOOR:
        out.append(f"fake {rt['fake']:.1%}, under the floor {h.FAKE_SUCCESS_FLOOR:.1%}")
    noisy = any(r.judge not in ("fake", h.REAL) for r in results)
    if whole and noisy and rt["noisy"] < h.SUCCESS_FLOOR:
        out.append(f"noisy {rt['noisy']:.1%}, under the floor {h.SUCCESS_FLOOR:.1%}")
    return out


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
                  verbose: bool, ctx, real_cache: str = "") -> list:
    """`name` in its own worker process, bounded by `timeout`: the worker's
    own results, or a "failed" row per judge naming a crash or a timeout."""
    out_q = ctx.Queue()
    p = ctx.Process(target=_run_flow_worker,
                    args=(name, seeds, fast, str(workdir), out_q, real_cache), daemon=True)
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
        return _crash_results(name, seeds, reason, bool(real_cache))
    if payload[0] == "done":
        return payload[1]
    cause = payload[2] if len(payload) > 2 else ""
    cause = cause.strip().splitlines()[-1] if cause.strip() else "unknown error"
    return _crash_results(name, seeds, f"apply_matrix: {name}'s worker failed: {cause}",
                          bool(real_cache))


def _run_parallel(flows, seeds: tuple, fast: bool, jobs: int, flow_timeout: float,
                  workdir: Path, *, verbose: bool = False, real_cache: str = "") -> list:
    """Every flow in `flows`, one worker process per flow, `jobs` running at
    once; the combined rows put back in `flows`' own order (each flow's own
    rows already come back in judge order from `run_matrix`), so the output
    matches the serial run's regardless of which worker finishes first."""
    ctx = mp.get_context("spawn")
    names = [f.name for f in flows]
    per_flow: dict[str, list] = {}

    def _task(name: str) -> None:
        per_flow[name] = _run_one_flow(name, seeds, fast, workdir, flow_timeout, verbose, ctx,
                                       real_cache)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        list(ex.map(_task, names))
    results = []
    for name in names:
        results.extend(per_flow[name])
    return results


# --- the real judge's recording ----------------------------------------------------------------

def _recorded_now(h, results) -> list[str]:
    """The flows a recording ran to its end (`results`) that the registry
    still marks `recorded=False`: the flags to flip, or the replay keeps
    leaving them out."""
    ran = {r.flow for r in results}
    return [f.name for f in h.FLOWS if f.name in ran and f.replayable and not f.recorded]


def _record_real(h, flows, mode: str, cache: Path, workdir: Path, *, fast: bool,
                 verbose: bool, json_out: str) -> int:
    """`--real record` or `--real dry`: the real judge's column alone, one
    run per flow in one process (`apply_harness.run_real`: the cache is one
    file and the spend counter is this process's), under the cap
    `AUTO_APPLY_RECORD_USD_CAP`. The cap stops the column: the flows left
    unrecorded are listed. A dry run answers with the fake at each
    request's estimated size into a temp copy of the cache and prints that
    copy's path, which `--real replay --real-cache` reads back."""
    import jev
    started = time.monotonic()
    try:
        rj = h.real_judge(mode, cache)
    except (jev.JevUnavailable, ValueError) as e:
        # no key, or a cap that is unset for a live recording or no amount
        # above 0: nothing is asked
        print(f"apply_matrix: {e}", file=sys.stderr)
        return 2
    before = jev.usage()
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
                if verbose:
                    print(f"{r.flow} {r.judge}: {'ok' if r.ok else 'NO'} {r.status} "
                          f"({r.reason}) {r.seconds}s {r.judge_requests} request(s)",
                          flush=True)
            col = h.run_real(flows, rj, browser=browser, server=server, workdir=workdir,
                             fast=fast, progress=progress)
            browser.close()
    finally:
        server.stop()
    after = jev.usage()
    requests = after["requests"] - before["requests"]
    tokens = after["input_tokens"] - before["input_tokens"]
    if col.results:
        print(h.summary(col.results))
    live = "estimated live" if mode == "dry" else "live"
    print(f"real judge ({mode}): {len(col.results)} of {len(flows)} flow(s) run in "
          f"{time.monotonic() - started:.0f}s; {live} requests {requests}, {tokens} input "
          f"tokens, {jev.usd_for(tokens):.4f} USD (cap {rj.cap.cap_usd:.4f} USD); cache {rj.cache}")
    if col.stopped:
        print(f"apply_matrix: {col.stopped}; left unrecorded: {', '.join(col.unrecorded)}",
              file=sys.stderr)
    flip = _recorded_now(h, col.results) if mode == "record" else []
    if flip:
        # the replay leaves a `recorded=False` flow out until its flag is
        # flipped (final review C N4)
        print(f"apply_matrix: recorded now, still marked recorded=False in "
              f"tests/apply_harness.py (flip each to recorded=True): {', '.join(flip)}")
    if json_out:
        rows = [{k: v for k, v in asdict(r).items() if k != "actions"} for r in col.results]
        Path(json_out).write_text(json.dumps({"results": rows, "unrecorded": col.unrecorded,
                                              "stopped": col.stopped, "requests": requests,
                                              "input_tokens": tokens}, indent=2),
                                  encoding="utf-8")
    return 1 if h.rates(col.results)["breaks"] else 0


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
                         "a failure row (default: 2x the slowest flow's time for the judge count)")
    ap.add_argument("--real", choices=("record", "replay", "dry"), default="",
                    help="the real judge's column: replay adds one run per flow over the "
                         "cache beside the fake and noisy runs; record (through "
                         "scripts/jev_record.ps1 -Target matrix) and dry run that column "
                         "alone, serially, under AUTO_APPLY_RECORD_USD_CAP")
    ap.add_argument("--real-cache", default="",
                    help="the real judge's cache (default "
                         "tests/fixtures/jev_cache/matrix_cache.json)")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="apply-matrix-") as tmp:
        tmp_path = Path(tmp)
        _isolate(tmp_path)
        import apply_harness as h
        flows = h.FLOWS
        if args.flows:
            wanted = {n.strip() for n in args.flows.split(",") if n.strip()}
            flows = tuple(f for f in h.FLOWS if f.name in wanted)
        real_cache = Path(args.real_cache) if args.real_cache else h.REAL_CACHE
        if args.real in ("record", "dry"):
            return _record_real(h, flows, args.real, real_cache, tmp_path,
                                fast=not args.real_timing, verbose=args.verbose,
                                json_out=args.json)
        noisy_seeds = h.NOISY_SEEDS[:max(0, args.seeds)]
        real = h.real_judge("replay", real_cache).judge if args.real == "replay" else None
        judge_list = h.judges(noisy_seeds, real=real)
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
                                    tmp_path, verbose=args.verbose,
                                    real_cache=str(real_cache) if real is not None else "")
        print(h.summary(results))
        if real is not None:
            rows = [r for r in results if r.judge == h.REAL]
            missed = [r.flow for r in rows if r.replay_misses]
            apart = [f.name for f in flows if not f.replayable]
            unrecorded = [f.name for f in flows if f.replayable and not f.recorded]
            print(f"real judge: replay of {real_cache}: {sum(r.replay_misses for r in rows)} "
                  f"miss(es) over {len(rows)} flow(s)"
                  + (f"; flows with a miss: {', '.join(missed)}" if missed else "")
                  + (f"; left out, their text changes with the clock: {', '.join(apart)}"
                     if apart else "")
                  + (f"; left out, not recorded yet (their recorded=False flags in "
                     f"tests/apply_harness.py flip after the next recording): "
                     f"{', '.join(unrecorded)}" if unrecorded else ""))
        print(f"the suite's pinned floors (fake and seeds {h.SUITE_SEEDS[0]} to "
              f"{h.SUITE_SEEDS[-1]}): noisy {h.SUCCESS_FLOOR:.1%}, fake "
              f"{h.FAKE_SUCCESS_FLOOR:.1%}; {len(flows)} flows x {len(judge_list)} judges in "
              f"{time.monotonic() - started:.0f}s")
        # the summary before the stderr lines below, when both go to one file
        sys.stdout.flush()
        crashed = _crashed(results)
        if crashed:
            print(f"apply_matrix: {crashed} run(s) lost to a crashed or hung worker, each "
                  "counted as a miss", file=sys.stderr)
        if args.json:
            rows = [{k: v for k, v in asdict(r).items() if k != "actions"} for r in results]
            Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        failed = _failures(h, results, whole=not args.flows)
        if failed:
            print(f"apply_matrix: FAILED: {'; '.join(failed)}", file=sys.stderr)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
