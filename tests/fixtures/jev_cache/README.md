# Jev replay caches

Two committed caches hold the real judge's answers over synthetic fixtures,
one recorded answer set per request, keyed by the sha256 of the canonical
`{"state", "questions"}` JSON (`local/jev.py`, `ReplayJev`). They hold fixture
text and judgments only. A change to a fixture or a question shape changes the
request key, and the replay then names the miss.

| cache | requests of | replay |
|---|---|---|
| `cache.json` | the runner tests (`tests/test_apply_run.py`, `tests/test_apply_run_boundaries.py`) | `scripts/jev_record.ps1 -Mode replay` |
| `matrix_cache.json` | the flow matrix's real column (`scripts/apply_matrix.py --real`), one run per registered flow | `scripts/jev_record.ps1 -Target matrix -Mode replay` |

`cache.json` was first recorded 2026-09-22 (SP8, jev-1.13.0, 38 requests) and
re-recorded 2026-09-25 (SP8b, 72 requests). `matrix_cache.json` was recorded
2026-09-25 (SP8b, 322 requests over the 106 flows a replay runs). Both hold
only the requests today's replay reaches: an entry no replay asks for is
dropped after a re-record. The third target, the page read over the local captures
(`tests/test_capture_reads.py`), keeps its cache beside the captures in
`tests/fixtures/local_captures/_jev/`: real third-party pages stay on the
machine that holds them, and so do their answers.

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

Every live entry point honours `AUTO_APPLY_RECORD_USD_CAP` (default 1.00 USD;
`-Cap` sets it). `jev.SpendCap` checks each request before it leaves: when the
spend so far plus the request's estimated cost (chars/3 tokens at 0.042 USD
per million input tokens) would pass the cap, the request is refused and every
later one too. A runner test the cap stops skips with the reason; the matrix
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
letter and a search box's match are compared in code, two sources that type
the same words pool their probability (`apply_judge.pooled_confidence`), and
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
`outcomes.jsonl` is ignored by git; the two caches are committed.
