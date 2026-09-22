# Jev replay cache for the runner tests

`cache.json` holds one recorded answer set per request the runner tests
(`tests/test_apply_run.py`, `tests/test_apply_run_boundaries.py`) make to Jev,
keyed by the sha256 of the canonical `{"state", "questions"}` JSON
(`local/jev.py`, `ReplayJev`). It is committed once a key holder records it;
until then the file is absent and the tests run on `FakeJev`.

`AUTO_APPLY_TEST_JEV` picks the judge for every runner in those two modules:

| value | judge | needs |
|---|---|---|
| unset or `fake` | `FakeJev()` (word overlap, deterministic) | nothing |
| `record` | `ReplayJev(TypeSafeJev(), cache.json)`: a miss asks the live model once and stores the answer | `TYPESAFE_API_KEY`, a small spend |
| `replay` | `ReplayJev(None, cache.json)`: a miss fails the test naming the fixture and the re-record command | the committed cache |

`AUTO_APPLY_JEV_CACHE` moves the cache file; `outcomes.jsonl` is written
beside it.

## Commands (Git Bash, from the repo root)

Record, with the key exported for that one process:

    AUTO_APPLY_TEST_JEV=record QT_QPA_PLATFORM=offscreen python -m pytest tests/test_apply_run.py tests/test_apply_run_boundaries.py -q

Replay, no key and no network:

    AUTO_APPLY_TEST_JEV=replay QT_QPA_PLATFORM=offscreen python -m pytest tests/test_apply_run.py tests/test_apply_run_boundaries.py -q

Tuning printout (per question kind: choices, the spread of confidence or
noul as a histogram, the current threshold from `local/apply_judge.py`, how
many answers fall on the wrong side of it; then every test's outcomes and
divergences):

    python scripts/jev_thresholds.py

`scripts/jev_record.ps1 [-Mode record|replay] [-Cap 1.00]` runs the same from
PowerShell, exporting `TYPESAFE_API_KEY` from `.env` for one run when it is
not already set.

## Cost guard (record mode)

Before the tests run, one line states how many tests use the `jev_judge`
fixture, how many requests are already cached (they replay for free) and the
cap. The harness measures live spend per test through `jev.usage()`; once it
passes `AUTO_APPLY_RECORD_USD_CAP` (default 1.00 USD) the remaining tests skip
with the reason and the cache keeps everything recorded so far. A dry run of
the whole suite with the fake standing in for the live model made 40 unique
requests over 93 tests, so one recording should sit well under the default
cap. Without the key, record mode skips every test with the reason.

## Divergences and `outcomes.jsonl`

The fixtures were written against the fake's answers (0.9 / 0.1, one option at
1.0). The real model may judge a fixture differently; that is what the
recording is for. In `record` and `replay` mode every test writes one JSON
line: the test id, every answer (with the fake's answer beside it), every
terminal outcome the runner reached (status, reason), and a failed assertion
becomes an xfail carrying the divergence text, so one run reports every
divergence at once. In `fake` mode nothing is written and assertions are hard.
`outcomes.jsonl` is ignored by git; `cache.json` is meant to be committed.
