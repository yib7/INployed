# Jev replay caches

Two committed caches hold the real judge's answers over synthetic fixtures,
one recorded answer set per request, keyed by the sha256 of the canonical
`{"state", "questions"}` JSON (`local/jev.py`, `ReplayJev`). They hold fixture
text and judgments only. A change to a fixture or a question shape changes the
request key, and the replay then names the miss.

| cache | requests of | replay |
|---|---|---|
| `cache.json` | the runner tests (`tests/test_apply_run.py`, `tests/test_apply_run_boundaries.py`) and the screening set's real judge (`tests/test_screening.py`) | `scripts/jev_record.ps1 -Mode replay` |
| `matrix_cache.json` | the flow matrix's real column (`scripts/apply_matrix.py --real`), one run per registered flow | `scripts/jev_record.ps1 -Target matrix -Mode replay` |

`cache.json` was first recorded 2026-09-22 (SP8, jev-1.13.0, 38 requests),
re-recorded 2026-09-25 (SP8b, 72 requests) and again 2026-09-26 (cycle 18 SP6,
276 requests, the screening set's among them). `matrix_cache.json` was recorded
2026-09-25 (SP8b, 322 requests over the 106 flows a replay runs) and again
2026-09-26 (cycle 18 SP6, 350 requests over 115 flows). Both hold
only the requests today's replay reaches: an entry no replay asks for is
dropped after a re-record. The third target, the page read over the local captures
(`tests/test_capture_reads.py`), keeps its cache beside the captures in
`tests/fixtures/local_captures/_jev/`: real third-party pages stay on the
machine that holds them, and so do their answers.

## The recording day

Every request that lists the fact catalog carries today's date (the `today`
fact, `local/apply_facts.py`), so the same request made on another day has
another key. A record or replay run reads today as `RECORDED_TODAY` in
`tests/jev_harness.py`, 2026-09-25: the runner tests through the `jev_judge`
fixture, the matrix's real column through `apply_harness.hermetic`. A
recording made on a later day runs pinned to that date too (the 2026-09-26 one
did), so both caches replay on any day. Moving the constant turns every entry
into a miss; a change to it re-records both caches. The fake and noisy judges and production read the
real date. The captures' page reads list no facts, so their cache needs no
pin.

## The runner tests' judge

`AUTO_APPLY_TEST_JEV` picks the judge for every runner in the two modules:

| value | judge | needs |
|---|---|---|
| unset or `fake` | `FakeJev()` (word overlap, deterministic) | nothing |
| `record` | `ReplayJev(SpendCap(TypeSafeJev(), cap), cache.json)`: a miss asks the live model once and stores the answer | `TYPESAFE_API_KEY`, a small spend |
| `replay` | `ReplayJev(None, cache.json)`: a miss fails the test naming the fixture and the re-record command | the committed cache |

`AUTO_APPLY_RECORD_DRY=1` turns `record` into a dry run: the fake answers at
each request's estimated size (`jev.DryRun`) into a temp copy of the cache,
with no key: the request count and the spend a recording would make.
`AUTO_APPLY_JEV_CACHE` moves the cache file; `outcomes.jsonl` is written
beside it.

## The matrix's real column

`scripts/apply_matrix.py --real record|replay|dry` adds the real judge beside
the fake and the noisy seeds. A recording (or a dry run) runs the real column
alone, one flow at a time in one process, and stops starting flows at the cap;
a replay runs in the worker pool with the rest. `ticker_page` is left out of a
replay: its page text changes with the clock, so no recorded key can hit.

A flow marked `recorded=False` in `tests/apply_harness.py` is one the cache has
no answers for yet: a replay leaves it out and names it. A recording runs it
and prints it as a flag to flip; after the next recording, set each one it
names to `recorded=True`, so the replay covers it. A replay that misses a
request exits 1.

## Pruning to the replayed keys (SP8)

Over the cycles a cache picks up keys no test replays any more: a fixture
changed shape, a test was removed, a flow was renamed. `-Prune` on
`scripts/jev_record.ps1` (`-Mode replay` only, `-Target runner` or `matrix`)
rewrites the target's cache to keep only the keys that replay actually served.

It is safe by construction: the replay's shared `ReplayJev` records every key
it serves (`local/jev.py`, `used_keys`), and the rewrite (`jev.prune_cache`)
refuses, naming the reason, unless the run had 0 replay misses and 0 test
failures. A miss or a failure means the run may not have reached every key a
clean pass would, and pruning on it could drop one a passing test still
needs -- so nothing is written, and the cache is left exactly as it was.

For the runner target, the shared replay lives for the whole pytest process
(one process covers every file in `RUNNER_TESTS`), so its `used_keys` already
covers all four modules; at session finish (`conftest_jev.pytest_sessionfinish`)
the keys are written to `used_keys.json` beside `outcomes.jsonl` (gitignored),
and, when `-Prune` set `AUTO_APPLY_JEV_PRUNE`, the cache is pruned there and
then. For the matrix target, `--jobs` runs one worker process per flow, so
each worker's real judge (`apply_harness.real_judge("replay", ...)`) reports
its own `used_keys` back on its "done" message; `scripts/apply_matrix.py`
folds every worker's keys together before `--real-prune` (which the `.ps1`
switch passes through) rewrites `--real-cache`.

Prune after a live recording, never instead of one: it only ever removes
keys, so run it once the new flows are recorded and every test that should
replay does.

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/jev_record.ps1 -Mode replay -Prune
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/jev_record.ps1 -Target matrix -Mode replay -Prune

## Commands (from the repo root)

Every live recording goes through `scripts/jev_record.ps1`, which loads the
key into that one process and drops it afterwards:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/jev_record.ps1 -Target runner -Cap 0.10
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/jev_record.ps1 -Target matrix -Cap 0.20
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/jev_record.ps1 -Target captures -Cap 0.05

Add `-Dry` for the estimate (no key), or `-Mode replay` for the replay (no key,
no network). The replays also run directly:

    AUTO_APPLY_TEST_JEV=replay QT_QPA_PLATFORM=offscreen python -m pytest tests/test_apply_run.py tests/test_apply_run_boundaries.py -q
    python scripts/apply_matrix.py --real replay --seeds 0

Tuning printout (per question kind: choices, the spread of confidence or noul
as a histogram, the current threshold from `local/apply_judge.py`, how many
answers fall on the wrong side of it; with `--reads`, the combined page reads
against the page-read gate):

    python scripts/jev_thresholds.py --cache tests/fixtures/jev_cache/cache.json --cache tests/fixtures/jev_cache/matrix_cache.json

## Cost guard

Every live entry point honours `AUTO_APPLY_RECORD_USD_CAP` (`-Cap` sets it).
A live recording names its cap, at most what the spend ledger has left under
the limit: `jev_record.ps1` refuses a live run without `-Cap`, and the Python
entry points refuse one without the variable. Only a dry run or a replay
(neither spends anything) has a default, 0.88 USD, what the cycle's approval
had left under its limit after SP8b. A cap that is no finite amount above 0
(NaN, inf, 0) is refused. `jev.SpendCap` checks each request before it
leaves: when the spend so far plus the request's estimated cost (chars/3
tokens at 0.042 USD per million input tokens) would pass the cap, the request
is refused and every later one too. A request that fails after it left (a
timeout, a server error) counts at its estimate, since the service may have
billed it. A runner test the cap stops skips with the reason; the matrix
lists the flows it left unrecorded; a capture read skips. The cache keeps
every answer recorded before the stop. Before the tests run, one line states
how many tests use the `jev_judge` fixture, how many requests are already
cached (they replay for free) and the cap. Without the key, record mode skips
every test with the reason.

## What the recordings taught the questions

Every runner takes a no-op `sleep` (`_no_sleep` in `test_apply_run.py`): a
judgment that diverges from the fake must never wait out an inbox poll
inside the pytest timeout. The first live run found four question shapes the
fake could not: the from-site inbox question now names the ATS and the
company (`apply_judge.ATS_NAMES`), the page state sends each button as
`{n, text}` without the extractor's `kind_hint`, the first request asks an
option pick only where `quick_map` has the value, and a generated answer is
verified against its draft in code. The SP8b recordings added five: the
"sends" criterion names a click that also creates the account, a pasted cover
letter and a search box's match are compared in code, two name sources that
type the same words pool their probability (`apply_judge.pooled_confidence`;
an answer-bank entry, a Yes or a number never pools), and
the GitHub fact's description names a portfolio. The thresholds header in
`local/apply_judge.py` records the live distribution per gate.

## Divergences and `outcomes.jsonl`

The fixtures were written against the fake's answers (0.9 / 0.1, one option at
1.0). The real model may judge a fixture differently; that is what the
recording is for. In `record` and `replay` mode every test writes one JSON
line: the test id, every answer (with the fake's answer beside it), every
terminal outcome the runner reached (status, reason), and a failed assertion
becomes an xfail carrying the divergence text, so one run reports every
divergence at once. In `fake` mode nothing is written and assertions are hard.
`outcomes.jsonl` and `used_keys.json` (SP8, the run's used-key set for
`-Prune`) are both ignored by git; the two caches are committed.
