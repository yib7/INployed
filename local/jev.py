"""Jev (TypeSafe System One) client wrapper for the auto-apply loop.

Every judgment the apply loop makes goes through one `judge(state, questions)`
call that returns an `Answer` per question id. Three implementations share that
shape:

- `TypeSafeJev` wraps `typesafe_sdk.TypeSafeClient` with the model pinned to
  `jev-1.13.0` (thresholds are tuned per version; the docs say pin) and keeps a
  process-wide usage counter (`usage()`, `reset_usage()`) beside a lifetime
  one no reset touches (`total_usage()`, what the spend guard reads).
- `FakeJev` is deterministic and key-free: word overlap between the question
  and the state. It drives the loop in tests and dry runs; its rules are in the
  class docstring so fixtures can be written against them.
- `ReplayJev(inner, cache_path)` replays a recorded answer for a request it has
  seen and records through `inner` on a miss, so a committed cache can stand in
  for the live model.
- `jev_doubles.NoisyJev(inner, seed)` bends another judge's answers the way a real misread
  would (a neighbouring page state, lower confidences, two buttons' roles
  exchanged, a field mapping dropped), the same way for the same request. The
  flow matrix and the invariant harness run on it; nothing in production does.
- `Guarded(inner)` is how the runner holds its judge: a request the service
  could not answer is tried again for about a minute, and then a circuit
  breaker opens and raises `JudgeOutage`.

`get(mode)` is the factory the runner and the dashboard call. Raw question
dicts in the HTTP shape (`{"type": ..., "instructions": ..., "criteria": ...}`)
pass straight through to the SDK, which accepts them alongside its typed
objects. `typesafe_sdk` is imported lazily inside `TypeSafeJev` so this module
imports without it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jsonutil import atomic_write_json, read_json_dict

log = logging.getLogger("jev")

REPO_ROOT = Path(__file__).resolve().parent.parent

MODEL = "jev-1.13.0"
PRICE_USD_PER_MTOK = 0.042          # $42 per billion input tokens; output is free
KEY_ENV = "TYPESAFE_API_KEY"
MODE_ENV = "AUTO_APPLY_JEV_MODE"
CACHE_ENV = "AUTO_APPLY_JEV_CACHE"
DEFAULT_CACHE = "tests/fixtures/jev_cache/cache.json"
CONSOLE_KEYS_URL = "console.typesafe.ai/keys"
# The API root, passed to the SDK by name so a TYPESAFE_BASE_URL in the
# environment can never send the key and the page summaries to another host
BASE_URL = "https://api.typesafe.ai"

MODES = ("typesafe", "fake", "replay")


class JevUnavailable(RuntimeError):
    """The live judge cannot run: no API key, or the SDK is not installed."""


@dataclass(frozen=True)
class Answer:
    """One answer, whatever the question type.

    `kind` is "noul", "choice" or "score". A noul carries `noul` (the yes
    probability) and nothing else. A choice carries `choice` plus the full
    `probabilities` over the option names and a `confidence`. A score carries
    `score` (the probability-weighted level, may sit between levels) plus
    `probabilities` keyed by the level index as a string and a `confidence`.
    """
    kind: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Answer:
        return cls(kind=str(raw["kind"]),
                   noul=raw.get("noul"),
                   choice=raw.get("choice"),
                   score=raw.get("score"),
                   probabilities=dict(raw.get("probabilities") or {}),
                   confidence=raw.get("confidence"))


class Jev(Protocol):
    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        ...


# --- usage counter (process-wide) -----------------------------------------------

_USAGE = {"requests": 0, "input_tokens": 0}
# The lifetime counter: the same counts, and `reset_usage()` never touches it.
# A spend guard (`SpendCap`) and a recording's summary measure from it, so a
# reset anywhere in the process (a test, a per-run report) can never make
# them forget a live request that was already made.
_TOTAL = {"requests": 0, "input_tokens": 0}
# Requests a stand-in made at their estimated size (`DryRun`): nothing left
# the machine and nothing was billed, so neither counter above sees them. A
# dry run's own cap and estimate read them beside the live ones.
_SIMULATED = {"requests": 0, "input_tokens": 0}
# The scorer judges from worker threads, so every read and write of any
# counter holds this lock: no count is lost, and a reader never sees one half
# counted.
_USAGE_LOCK = threading.Lock()


def usage() -> dict:
    """{"requests", "input_tokens", "usd"} for every live request this process
    made since the last `reset_usage()`. The fake, a replay hit and a
    `DryRun` never count."""
    with _USAGE_LOCK:
        requests, tokens = _USAGE["requests"], _USAGE["input_tokens"]
    return {"requests": requests, "input_tokens": tokens, "usd": usd_for(tokens)}


def total_usage(*, include_simulated: bool = False) -> dict:
    """{"requests", "input_tokens", "usd"} for every live request this process
    ever made: `usage()` without its resets, so it only grows. A guard reads a
    delta of it. `include_simulated` adds the `DryRun` requests
    (`simulated_usage()`), for a dry run's estimate of what a recording
    would spend."""
    with _USAGE_LOCK:
        requests, tokens = _TOTAL["requests"], _TOTAL["input_tokens"]
        if include_simulated:
            requests += _SIMULATED["requests"]
            tokens += _SIMULATED["input_tokens"]
    return {"requests": requests, "input_tokens": tokens, "usd": usd_for(tokens)}


def simulated_usage() -> dict:
    """{"requests", "input_tokens", "usd"} for every `DryRun` request this
    process made, at their estimated size. Never billed; it only grows."""
    with _USAGE_LOCK:
        requests, tokens = _SIMULATED["requests"], _SIMULATED["input_tokens"]
    return {"requests": requests, "input_tokens": tokens, "usd": usd_for(tokens)}


def reset_usage() -> None:
    """Zero `usage()`; `total_usage()` and `simulated_usage()` keep their counts."""
    with _USAGE_LOCK:
        _USAGE["requests"] = 0
        _USAGE["input_tokens"] = 0


def count_usage(tokens: int, *, simulated: bool = False) -> None:
    """Add one live request of `tokens` input tokens to both process counters;
    with `simulated`, one `DryRun` request to the simulated counter alone."""
    tokens = max(0, int(tokens))
    with _USAGE_LOCK:
        if simulated:
            _SIMULATED["requests"] += 1
            _SIMULATED["input_tokens"] += tokens
            return
        _USAGE["requests"] += 1
        _USAGE["input_tokens"] += tokens
        _TOTAL["requests"] += 1
        _TOTAL["input_tokens"] += tokens


def usd_for(tokens: int | float) -> float:
    """What `tokens` input tokens cost at `PRICE_USD_PER_MTOK`."""
    return float(tokens) / 1_000_000 * PRICE_USD_PER_MTOK


# --- the request's size ----------------------------------------------------------

# Jev's limits (docs/superpowers/jev-complete-guide.md, "Limits & price"): 32k
# tokens for the state and the single longest question, 64k for the state and
# every question. The estimate is the compact JSON's length over
# CHARS_PER_TOKEN, a low ratio for JSON (English prose runs about 4), and a
# request fits under SIZE_MARGIN of each limit.
STATE_TOKENS_MAX = 32_000
REQUEST_TOKENS_MAX = 64_000
CHARS_PER_TOKEN = 3.0
SIZE_MARGIN = 0.85


def estimate_tokens(value: Any) -> int:
    """The tokens `value` (a state or one question) is estimated to take."""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def request_size(state: Any, questions: Mapping[str, Any]) -> tuple[int, int]:
    """(the state and the longest question, the state and every question),
    in estimated tokens."""
    base = estimate_tokens(state)
    sizes = [estimate_tokens(q) for q in questions.values()]
    return base + max(sizes, default=0), base + sum(sizes)


def _sized_to_fit(longest: int, whole: int) -> bool:
    return longest <= STATE_TOKENS_MAX * SIZE_MARGIN and whole <= REQUEST_TOKENS_MAX * SIZE_MARGIN


# Jev takes at most this many options in one choice (the guide, "Choice"); the
# service answers a longer one with a 400 and the SDK checks nothing before it
# sends. A choice over about 360 options, well under the token limits, came
# back 400 every time.
CHOICE_OPTIONS_MAX = 255


class RequestRejected(ValueError):
    """A request Jev refuses whatever the moment (a choice past
    `CHOICE_OPTIONS_MAX`), refused before it is sent. `Guarded` raises it as a
    service's 400 passes through: never retried, and the breaker stays shut. The
    message names the question and the count, never the request's text."""


def _long_choices(questions: Mapping[str, Any]) -> list[tuple[str, int]]:
    """(question id, option count) for each choice past `CHOICE_OPTIONS_MAX`."""
    out = []
    for qid, q in questions.items():
        if isinstance(q, Mapping) and q.get("type") == "choice":
            n = len(q.get("criteria") or ())
            if n > CHOICE_OPTIONS_MAX:
                out.append((str(qid), n))
    return out


def request_fits(state: Any, questions: Mapping[str, Any]) -> bool:
    """Is the request under `SIZE_MARGIN` of both of Jev's limits, with no choice
    past `CHOICE_OPTIONS_MAX` options?"""
    return _sized_to_fit(*request_size(state, questions)) and not _long_choices(questions)


# --- the outage guard -------------------------------------------------------------

# The retries of a judge request the service could not answer (the SDK's own
# are off, `TypeSafeJev`): about a minute over three more attempts.
RETRY_DELAYS_S = (5.0, 15.0, 40.0)
# scoring and the tailor have an LLM fallback: their judges retry briefly (`jev_switch.client`)
QUICK_RETRY_DELAYS_S = (1.0, 3.0)
RETRY_AFTER_CAP_S = 60.0     # a longer Retry-After reads as the judge being down
_BUSY_STATUS = frozenset((408, 409, 425, 429))
_OVERLOADED_STATUS = frozenset((503, 529))      # the service, whoever asks
# the key or the account (401, 402, 403), or a model the service retired or
# renamed (404, 410): no wait mends either, and every job would fail on it
_REFUSED_STATUS = frozenset((401, 402, 403, 404, 410))


class JudgeOutage(RuntimeError):
    """The judge stayed unreachable through the retries, asked for a longer
    wait than `RETRY_AFTER_CAP_S`, or refused the key or the model. `kind` names the
    error's class and status, never its message (a service error can quote
    the request, which carries the page and the applicant's facts)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def error_kind(e: BaseException) -> str:
    """The error's class, and its status when there is one ("TypeSafeRateLimitError 429")."""
    status = getattr(e, "status", None)
    return f"{type(e).__name__} {status}" if isinstance(status, int) else type(e).__name__


def _transient(e: BaseException) -> bool:
    status = getattr(e, "status", None)
    if isinstance(status, int):
        return status in _BUSY_STATUS or status >= 500
    return isinstance(e, (ConnectionError, TimeoutError))


def request_fault(e: BaseException) -> bool:
    """Could the request itself have caused the error: a 5xx other than
    503 and 529, or a timeout (408 too; a large request can time out)? A
    busy or overloaded service (409, 425, 429, 503, 529) never did, and a
    dropped connection is most often the network's (a laptop off its
    Wi-Fi)."""
    status = getattr(e, "status", None)
    if isinstance(status, int):
        return status == 408 or (status >= 500 and status not in _OVERLOADED_STATUS)
    return isinstance(e, TimeoutError)


def retry_after_s(e: BaseException) -> float | None:
    """The wait the service asked for: the SDK's `retry_after_ms`, else the
    `retry-after-ms` or `retry-after` header in seconds; None when it asked
    for none (a date form reads as none)."""
    ms = getattr(e, "retry_after_ms", None)
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0:
        return float(ms) / 1000
    headers = getattr(e, "headers", None)
    try:
        raw = headers.get("retry-after-ms") if headers is not None else None
        if raw:
            return max(0.0, float(raw) / 1000)
        raw = headers.get("retry-after") if headers is not None else None
        if raw:
            return max(0.0, float(raw))
    except (AttributeError, TypeError, ValueError):
        return None
    return None


class Guarded:
    """A judge with the run's retries and a circuit breaker.

    A request the service could not answer (a status of 408, 409, 425, 429
    or 5xx, a dropped connection, a timeout) is tried again after each of
    `delays`, or after the service's Retry-After when that is longer. When
    the last try fails too, when the service asks for a wait over
    `RETRY_AFTER_CAP_S`, or when it refuses the key (401, 402, 403) or no
    longer has the model (404, 410), the breaker opens: `down` names the
    error's class and status, `refused` says it was the key or the model
    (no wait mends either), and every later request
    raises `JudgeOutage` at once, so the run can hand its job back to the
    queue and stop the drain. Any other error (a request the service
    rejected, a bug) passes through as it was, and a choice past
    `CHOICE_OPTIONS_MAX` options raises `RequestRejected` before it is sent,
    as its 400 would. `answers` counts the requests answered (the runner
    sets it to 0 at each drain's start: an outage counts toward a job's cap
    only after the judge answered in the drain). `request_fault` says the
    request itself may have caused the outage (`request_fault(e)` on every
    try, with no Retry-After over the cap): only such an outage counts
    toward the cap. Attributes other than `judge` are the wrapped
    judge's."""

    def __init__(self, inner: Any, *, sleep: Callable[[float], None] = time.sleep,
                 delays: tuple[float, ...] = RETRY_DELAYS_S,
                 logger: logging.Logger | None = None):
        self.inner = inner
        self.sleep = sleep
        self.delays = tuple(delays)
        self.log = logger if logger is not None else log
        self.down = ""
        self.refused = False
        self.answers = 0
        self.request_fault = False

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        if self.down:
            raise JudgeOutage(self.down)
        longest, whole = request_size(state, questions)
        if not _sized_to_fit(longest, whole):
            # the mapping is sized before it is asked (`apply_judge.page_requests`);
            # any other request this large is named in the job's log
            self.log.warning("jev request estimated at %d tokens (the state and its longest "
                             "question) and %d in all, past %d%% of the %d and %d limits",
                             longest, whole, round(SIZE_MARGIN * 100), STATE_TOKENS_MAX,
                             REQUEST_TOKENS_MAX)
        for qid, n in _long_choices(questions):
            # the service answers it with a 400 every time, so it is refused
            # unsent, the way a 400 passes through below: no retry, the breaker shut
            why = f"jev choice {qid} has {n} options, past the {CHOICE_OPTIONS_MAX} a choice takes"
            self.log.warning("%s", why)
            raise RequestRejected(why)
        tries = len(self.delays) + 1
        fault = True            # every try failed with an error the request may cause
        for n in range(tries):
            try:
                got = self.inner.judge(state, questions)
                self.answers += 1
                return got
            except JudgeOutage:
                raise
            except Exception as e:      # noqa: BLE001  (sorted below; the rest pass through)
                kind = error_kind(e)
                refused = getattr(e, "status", None) in _REFUSED_STATUS
                if not refused and not _transient(e):
                    raise
                asked = retry_after_s(e)
                long_wait = asked is not None and asked > RETRY_AFTER_CAP_S
                fault = fault and request_fault(e) and not long_wait
                if refused or n + 1 >= tries or long_wait:
                    self.down, self.refused, self.request_fault = kind, refused, fault
                    self.log.warning("jev unavailable: %s after %d attempt(s); the breaker "
                                     "is open", kind, n + 1)
                    raise JudgeOutage(kind) from e
                wait = max(self.delays[n], asked or 0.0)
                self.log.warning("jev %s; attempt %d of %d in %.0f s", kind, n + 2, tries, wait)
                self.sleep(wait)
        raise JudgeOutage(self.down or "no attempt")    # never reached: the loop returns or raises

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


# --- the live client --------------------------------------------------------------

class TypeSafeJev:
    """The live judge over `typesafe_sdk.TypeSafeClient`.

    The key comes from the `api_key` argument, else `TYPESAFE_API_KEY` in the
    environment (`.env` reaches it through the dashboard's and the runner's
    `load_dotenv`). Construction raises `JevUnavailable` when the key or the SDK
    is missing, so a run refuses to start before it can park every job. No
    request is made until `judge()`. The SDK makes each request once (its
    retries are off: `Guarded` owns them); `transport` is the SDK's HTTP
    transport (a test's `httpx2.MockTransport`), the SDK's own when None.
    """

    def __init__(self, api_key: str | None = None, model: str = MODEL, *,
                 transport: Any = None):
        key = (api_key or os.environ.get(KEY_ENV, "")).strip()
        if not key:
            raise JevUnavailable(
                f"No TypeSafe API key. Create one at {CONSOLE_KEYS_URL}, then add the "
                f"row `{KEY_ENV}=<your key>` to .env (or paste it into Settings > "
                "Jev > TypeSafe API key) and restart the dashboard.")
        try:
            import typesafe_sdk
        except ImportError as exc:
            raise JevUnavailable(
                "The typesafe_sdk package is not installed: run "
                "`pip install typesafe-sdk` (it is in requirements.txt).") from exc
        self.model = model
        self.last_model: str | None = None
        extra = {"transport": transport} if transport is not None else {}
        # `Guarded` owns the retries (`RETRY_DELAYS_S`): the SDK's own are off,
        # so a judge that stays down costs four requests over about a minute,
        # never the SDK's three inside each of them
        self._client = typesafe_sdk.TypeSafeClient(
            api_key=key, model=model, retry=typesafe_sdk.RetryPolicy(max_retries=0),
            base_url=BASE_URL, **extra)

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        response = self._client.system_one(state, questions, model=self.model)
        tokens = int(getattr(response.usage, "input_tokens", 0) or 0)
        count_usage(tokens)
        self.last_model = getattr(response, "model", None)
        log.info("jev %s: %d question(s), %d input tokens",
                 self.last_model, len(questions), tokens)
        return {qid: _from_sdk(ans) for qid, ans in response.answers.items()}


def _from_sdk(ans: Any) -> Answer:
    kind = str(ans.type)
    if kind == "noul":
        return Answer(kind="noul", noul=float(ans.noul))
    probs = {str(k): float(v) for k, v in dict(ans.probabilities).items()}
    conf = float(ans.confidence) if ans.confidence is not None else None
    if kind == "choice":
        return Answer(kind="choice", choice=str(ans.choice), probabilities=probs,
                      confidence=conf)
    if kind == "score":
        return Answer(kind="score", score=float(ans.score), probabilities=probs,
                      confidence=conf)
    raise ValueError(f"unknown answer type {kind!r}")


# --- the deterministic fake ---------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
_PATH_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[\d+\]))*)`")
_INDEX_RE = re.compile(r"\[(\d+)\]")
_STOPWORDS = frozenset("""
a an the is are was were be been being am do does did done of in on at to for
from by with and or but it its this that these those as if then than has have
had which what who whom whose where when how why there here about into onto
over under also can could may might shall should will would you your yours
we our ours they them their he she his her him i me my mine
""".split())
_WHOLE_STATE_CAP = 20_000     # characters of serialised state the fake reads
_NOUL_MIN_WORD = 4
_FALLBACK_ORDER = ("none", "other", "leave_blank", "no_match")
# the escape of a settle question (`apply_judge.settle_questions`): the fake
# cannot read whether a saved answer settles a reworded question, so it
# always takes this one when it is listed
NOT_SETTLED = "not_settled"


class FakeJev:
    """A deterministic, key-free judge for tests and dry runs. Never used for a
    real application on purpose: it only matches words.

    Rules (fixtures are written against these; change them and the fixtures):

    - Text is tokenised into lowercase word sets (`[a-z0-9]+`, so `leave_blank`
      is the two words `leave` and `blank`). Instructions may be a string, an
      object or an array; every string inside counts. A boolean is never
      read: an object's entry whose value is a boolean is left out with its
      key (a button's `in_form` / `disabled` / `primary` flags, a field's
      `required`).
    - The state text is the slice(s) the instructions name with backticked
      paths (`sheet.work_authorization`, `field.options[0]`), serialised as
      JSON. When no named path resolves, it is the whole state as JSON, cut to
      its first 20,000 characters. The path names themselves are removed from
      the instruction text before tokenising.
    - Choice: each option scores the number of its distinct words (option name
      plus its description, stopwords dropped) that appear in the instruction
      words plus the state words. Highest wins; a tie keeps the earlier option.
      When no option scores above zero the winner is, in order of presence,
      `none`, `other`, `leave_blank`, `no_match`, else the first option.
      `probabilities` puts 1.0 on the winner and 0.0 elsewhere; `confidence`
      is 1.0.
    - Choice, `not_settled` (`NOT_SETTLED`): when it is one of the options
      it wins at 1.0, whatever the words: the fake cannot read whether a
      saved answer settles a question worded another way.
    - Choice, a named option table: a backticked path that resolves to a map
      sharing a key with the option names is the options' description table.
      Each option also scores against its own entry, and the table stays out
      of the state text.
    - Choice, `not_for`: in a structured description (or table entry) the
      words under a `not_for` key count against the option, one per word
      found in the context; the other entries count for it.
    - Noul: 0.9 when at least two distinct instruction content words (four or
      more letters, stopwords excluded) appear in the state text, else 0.1.
      `probabilities` is empty and `confidence` is None, as in the API.
    - Noul, structured criteria: when `criteria.true` is an object carrying
      `examples`, the state text is searched for each example phrase of
      `criteria.true` and of `criteria.false` (whole words, case-insensitive,
      typographic apostrophes read as plain ones); 0.9 when more `true`
      phrases than `false` phrases are found, else 0.1. The page read's
      Nouls (`apply_judge.read_questions`) are written this way.
    - Score: the level whose text overlaps the instruction plus state words
      most (a tie keeps the lower level); `score` is that level's index as a
      float, `probabilities` puts 1.0 on it (keyed by the index as a string)
      and `confidence` is 1.0.
    """

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            kind = str(q.get("type", ""))
            instr_text, slices = _fake_parts(state, q.get("instructions", ""))
            state_text = _join_slices(state, slices)
            context = _words(instr_text) | _words(state_text)
            if kind == "noul":
                examples = _noul_examples(q.get("criteria"))
                out[qid] = (self._noul_phrases(examples, state_text) if examples is not None
                            else self._noul(instr_text, state_text))
            elif kind == "choice":
                criteria = q.get("criteria") or {}
                tables = [t for t in slices
                          if isinstance(t, Mapping) and set(criteria) & set(t)]
                if tables:
                    rest = [x for x in slices if not any(x is t for t in tables)]
                    context = _words(instr_text) | _words(" ".join(_as_text(x) for x in rest))
                out[qid] = self._choice(criteria, context, tables)
            elif kind == "score":
                out[qid] = self._score(q.get("criteria") or [], context)
            else:
                raise ValueError(f"question {qid!r}: unknown type {kind!r}")
        return out

    @staticmethod
    def _noul(instr_text: str, state_text: str) -> Answer:
        content = {w for w in _words(instr_text) if len(w) >= _NOUL_MIN_WORD}
        hits = content & _words(state_text)
        return Answer(kind="noul", noul=0.9 if len(hits) >= 2 else 0.1)

    @staticmethod
    def _noul_phrases(examples: tuple[list[str], list[str]], state_text: str) -> Answer:
        yes, no = examples
        text = _plain(state_text)
        hits = sum(1 for e in yes if _phrase_in(e, text))
        misses = sum(1 for e in no if _phrase_in(e, text))
        return Answer(kind="noul", noul=0.9 if hits > misses else 0.1)

    @staticmethod
    def _choice(criteria: Mapping[str, Any], context: set[str],
                tables: list[Mapping] = ()) -> Answer:
        names = list(criteria)
        if not names:
            raise ValueError("a choice question needs at least one option")
        if NOT_SETTLED in criteria:
            return Answer(kind="choice", choice=NOT_SETTLED,
                          probabilities={n: (1.0 if n == NOT_SETTLED else 0.0) for n in names},
                          confidence=1.0)
        scores: dict[str, int] = {}
        for name, desc in criteria.items():
            pos, neg = _split_desc(desc)
            for table in tables:
                tpos, tneg = _split_desc(table.get(name))
                pos, neg = f"{pos} {tpos}", f"{neg} {tneg}"
            scores[name] = (len(_words(f"{name} {pos}") & context)
                            - len(_words(neg) & context))
        best = max(scores.values())
        if best > 0:
            winner = next(n for n in names if scores[n] == best)
        else:
            winner = next((n for n in _FALLBACK_ORDER if n in criteria), names[0])
        return Answer(kind="choice", choice=winner,
                      probabilities={n: (1.0 if n == winner else 0.0) for n in names},
                      confidence=1.0)

    @staticmethod
    def _score(criteria: list, context: set[str]) -> Answer:
        if not criteria:
            raise ValueError("a score question needs at least one level")
        overlaps = [len(_words(_flatten(level)) & context) for level in criteria]
        idx = overlaps.index(max(overlaps))
        return Answer(kind="score", score=float(idx),
                      probabilities={str(i): (1.0 if i == idx else 0.0)
                                     for i in range(len(criteria))},
                      confidence=1.0)


# typographic apostrophes read as the plain one before words are matched
# (the one table: apply_judge and apply_run read it too)
APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\uff07": "'"})


def _plain(text: str) -> str:
    return " ".join(str(text or "").translate(APOSTROPHES).lower().split())


def _phrase_in(phrase: str, text: str) -> bool:
    """`phrase` in `text` as whole words (both already lowercased)."""
    want = _plain(phrase)
    if not want:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(want) + r"(?![a-z0-9])", text) is not None


def _noul_examples(criteria: Any) -> tuple[list[str], list[str]] | None:
    """(the `true` example phrases, the `false` ones) of a Noul's structured
    criteria, or None when `criteria.true` carries no `examples`."""
    if not isinstance(criteria, Mapping):
        return None
    yes = criteria.get("true")
    if not isinstance(yes, Mapping) or "examples" not in yes:
        return None
    no = criteria.get("false")
    no_examples = no.get("examples") if isinstance(no, Mapping) else None
    return ([str(e) for e in yes.get("examples") or []],
            [str(e) for e in no_examples or []])


def _split_desc(value: Any) -> tuple[str, str]:
    """(the text that counts for an option, the text that counts against it):
    a structured description's `not_for` entry is the negative part."""
    if isinstance(value, Mapping):
        pos = " ".join(_flatten(v) for k, v in value.items() if k != "not_for")
        return pos, _flatten(value.get("not_for"))
    return _flatten(value), ""


def _no_bools(value: Any) -> Any:
    """`value` without its boolean entries: an object's key whose value is a
    boolean goes with it, an array's boolean items go. A boolean flag (a
    button's `in_form`, `disabled`, `primary`, a field's `required`) says
    nothing a word match could read, and its key would count as a word."""
    if isinstance(value, Mapping):
        return {k: _no_bools(v) for k, v in value.items() if not isinstance(v, bool)}
    if isinstance(value, (list, tuple)):
        return [_no_bools(v) for v in value if not isinstance(v, bool)]
    return value


def _flatten(value: Any) -> str:
    """Every string inside a str / object / array, space-joined; object keys are
    labels and are left out, and so are booleans."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _resolve(state: Any, path: str) -> tuple[bool, Any]:
    cur = state
    for part in path.split("."):
        head = _INDEX_RE.split(part)[0]
        if head:
            if not isinstance(cur, Mapping) or head not in cur:
                return False, None
            cur = cur[head]
        for idx in _INDEX_RE.findall(part):
            if not isinstance(cur, (list, tuple)) or int(idx) >= len(cur):
                return False, None
            cur = cur[int(idx)]
    return True, cur


_JSON_ESCAPES = (chr(92) + 'n', chr(92) + 'r', chr(92) + 't')


def _as_text(value: Any) -> str:
    """A string as itself, anything else as JSON with its newline, return
    and tab escapes turned into spaces (a JSON-escaped line start is a word
    boundary; without this the escape's letter glues onto the next word)."""
    if isinstance(value, str):
        return value
    text = json.dumps(_no_bools(value), ensure_ascii=False)
    for esc in _JSON_ESCAPES:
        text = text.replace(esc, ' ')
    return text


def _fake_parts(state: Any, instructions: Any) -> tuple[str, list[Any]]:
    """(instruction text with the backticked paths removed, the state values
    the instructions point at, in order)."""
    raw = _flatten(instructions)
    slices = []
    for path in _PATH_RE.findall(raw):
        ok, value = _resolve(state, path)
        if ok:
            slices.append(value)
    return _PATH_RE.sub(" ", raw), slices


def _join_slices(state: Any, slices: list[Any]) -> str:
    """The state text: the named slices, else the whole state, capped."""
    if slices:
        return " ".join(_as_text(v) for v in slices)
    return _as_text(state)[:_WHOLE_STATE_CAP]


# The page read's Nouls (`apply_judge.read_questions`), by the page kind each
# speaks for; `page_already_applied` speaks for none (its own park reason).
# `jev_doubles.NoisyJev` misreads them with the page state: a misread page state takes its
# Nouls along (`coherent_p`), and each is flipped on its own besides.
PAGE_KIND_NOULS: dict[str, tuple[str, ...]] = {
    "job_posting": ("page_job_description", "page_apply_entry"),
    "application_form": ("page_applicant_details",),
    "login_wall": ("page_sign_in",),
    "signup_form": ("page_create_account",),
    "review_page": ("page_review",),
    "confirmation": ("page_received",),
    "code_gate": ("page_code",),
    "captcha_or_bot_check": ("has_captcha",),
    "payment_request": ("page_payment",),
    "error_or_dead": ("page_closed", "page_error"),
}
READ_NOULS: frozenset[str] = frozenset(
    q for qs in PAGE_KIND_NOULS.values() for q in qs) | {"page_already_applied"}


# --- the replay cache ---------------------------------------------------------------

class ReplayJev:
    """Replays a recorded answer for a request it has seen; records through
    `inner` on a miss. With `inner=None`, a miss raises `JevUnavailable` and
    the cache remains unchanged.

    The key is the sha256 of the canonical JSON of `{"state", "questions"}`
    (sorted keys, no whitespace), so key order in the state does not matter and
    any change to a question does. The cache is one JSON object
    `{key: {question_id: answer}}` written atomically after every miss.

    `used_keys` is every key this instance served: a hit's key, and a
    miss's key once it is recorded. It is the run's own record of what it
    needed, so a cache carrying a stale key no test asks for any more can be
    told apart from one every key of which still earns its place
    (`prune_cache`).
    """

    def __init__(self, inner: Jev | None, cache_path: Path):
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.hits = 0
        self.misses = 0
        self.used_keys: set[str] = set()
        self._cache: dict[str, dict] | None = None

    @staticmethod
    def key_for(state: Any, questions: dict[str, dict]) -> str:
        canon = json.dumps({"state": state, "questions": questions}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, dict]:
        if self._cache is None:
            self._cache = read_json_dict(self.cache_path)
        return self._cache

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        cache = self._load()
        key = self.key_for(state, questions)
        hit = cache.get(key)
        if isinstance(hit, dict) and set(hit) == set(questions):
            self.hits += 1
            self.used_keys.add(key)
            return {qid: Answer.from_dict(raw) for qid, raw in hit.items()}
        self.misses += 1
        if self.inner is None:
            raise JevUnavailable("Jev replay cache miss; record this fixture explicitly first")
        answers = self.inner.judge(state, questions)
        cache[key] = {qid: a.to_dict() for qid, a in answers.items()}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.cache_path, cache)
        self.used_keys.add(key)
        return answers


class PruneRefused(RuntimeError):
    """`prune_cache` refused to rewrite the cache: a miss or a failed test in
    the run means its `used_keys` may not cover every key a clean pass would
    reach, and pruning on it could drop one a passing test still needs."""


def prune_cache(cache_path: Path, used_keys: set[str], *, misses: int = 0,
                failures: int = 0) -> tuple[int, int]:
    """Rewrite the cache at `cache_path` to keep only `used_keys`, atomically
    (`atomic_write_json`, the same tmp-file-then-replace every other cache
    write here uses). Raises `PruneRefused`, naming the reason, when `misses`
    or `failures` is not zero (a miss or a failed test means this run may not
    have reached every key a clean pass would) or when `used_keys` is empty (a
    run that served nothing is never a reason to empty the cache). Returns
    (the key count before, the key count after)."""
    if misses:
        raise PruneRefused(f"{misses} replay miss(es) in the run: a miss means the run did not "
                           f"see every key a clean pass would, so pruning on it could drop one "
                           f"a passing test still needs")
    if failures:
        raise PruneRefused(f"{failures} test failure(s) in the run: a failure may have kept a "
                           f"test from reaching every key it would reach on a clean pass")
    if not used_keys:
        raise PruneRefused("no keys were used in this run; refusing to prune to an empty cache")
    cache = read_json_dict(cache_path)
    before = len(cache)
    kept = {k: v for k, v in cache.items() if k in used_keys}
    atomic_write_json(cache_path, kept)
    return before, len(kept)


# --- a recording's spend cap ------------------------------------------------------------

RECORD_CAP_ENV = "AUTO_APPLY_RECORD_USD_CAP"
# A live recording names its cap (`AUTO_APPLY_RECORD_USD_CAP`, which
# `scripts/jev_record.ps1 -Cap` sets): at most what the spend ledger has left
# under the approval's limit, and only the person reading the ledger knows
# that, so `record_cap` refuses a live recording without it. A dry run spends
# nothing and takes this cap when it names none: what a 0.95 USD approval had
# left after a first recording's 0.0694 USD (rounded down), so its estimate
# stops where such a recording would have to.
DRY_RECORD_CAP_USD = 0.88


class SpendCapReached(JevUnavailable):
    """A recording reached its spend cap: no more live requests leave."""


def checked_cap(value: Any, *, source: str = RECORD_CAP_ENV) -> float:
    """`value` as a cap in USD, or ValueError when it is no finite amount
    above 0: a NaN cap never stops (no sum is greater than NaN), and an
    infinite one is none."""
    try:
        cap = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{source}={value!r}: expected a USD amount above 0") from None
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"{source}={value!r}: expected a finite USD amount above 0")
    return cap


def record_cap(env: Mapping[str, str] | None = None, *, live: bool = True) -> float:
    """The recording's cap in USD: `AUTO_APPLY_RECORD_USD_CAP` (ValueError
    for a value that is no finite amount above 0, `checked_cap`). Without
    it a live recording is refused (ValueError naming the variable); a dry
    run (`live` False) takes `DRY_RECORD_CAP_USD`."""
    raw = ((os.environ if env is None else env).get(RECORD_CAP_ENV) or "").strip()
    if raw:
        return checked_cap(raw)
    if live:
        raise ValueError(f"{RECORD_CAP_ENV} is not set: a live recording names its cap, at most "
                         f"what the spend ledger has left under the limit "
                         f"(scripts/jev_record.ps1 -Cap <USD>)")
    return DRY_RECORD_CAP_USD


class SpendCap:
    """The live judge a recording asks through (`ReplayJev(SpendCap(live,
    cap), cache)`). It counts the live spend since it was made
    (`total_usage()`, so a `reset_usage()` anywhere in the process never
    lowers it), and a request whose estimated cost (`request_size`, the
    whole request, at `PRICE_USD_PER_MTOK`) would take that spend past
    `cap_usd` raises `SpendCapReached` before it leaves; so does every
    request after. A request that raised before the counter saw it (a
    timeout or a server error after the service may have billed it) counts
    at its estimate (`unbilled_usd`), and so does an answered request whose
    usage reported no input tokens, so the cap never runs behind. The check
    and the request are one step under a lock: two threads never both pass
    the check on the same spend.

    Over a `DryRun` (`inner.simulated`) the cap also counts the simulated
    requests, so a dry run stops where a recording would; a cap over a live
    judge counts real requests only, and every real request counts either way.
    `reached` says the cap stopped a request; `refused` counts them;
    `reason` is the first refusal's text. `cap_usd` must be a finite amount
    above 0 (`checked_cap`)."""

    def __init__(self, inner: Jev, cap_usd: float):
        self.inner = inner
        self.cap_usd = checked_cap(cap_usd, source="cap_usd")
        self.simulated = bool(getattr(inner, "simulated", False))
        self._start = self._counted()
        self._lock = threading.Lock()
        self.reached = False
        self.refused = 0
        self.reason = ""
        self.failed = 0             # requests that raised with no count of their own
        self.unbilled_usd = 0.0     # their estimated cost, and that of a zero-token answer

    def _counted(self) -> dict:
        return total_usage(include_simulated=self.simulated)

    @property
    def spent_usd(self) -> float:
        return max(0.0, self._counted()["usd"] - self._start["usd"]) + self.unbilled_usd

    @property
    def requests(self) -> int:
        return max(0, self._counted()["requests"] - self._start["requests"]) + self.failed

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        with self._lock:
            spent = self.spent_usd
            next_usd = usd_for(request_size(state, questions)[1])
            if self.reached or spent + next_usd > self.cap_usd:
                self.refused += 1
                reason = (f"{RECORD_CAP_ENV} reached: {spent:.4f} USD spent over "
                          f"{self.requests} live request(s), the next estimated at "
                          f"{next_usd:.4f} USD, cap {self.cap_usd:.4f} USD")
                if not self.reached:
                    self.reached, self.reason = True, reason
                raise SpendCapReached(reason)
            before = self._counted()
            try:
                answers = self.inner.judge(state, questions)
            except SpendCapReached:
                raise
            except BaseException:
                if self._counted()["requests"] == before["requests"]:
                    # the request may have been billed before it failed
                    self.failed += 1
                    self.unbilled_usd += next_usd
                raise
            after = self._counted()
            if (after["requests"] > before["requests"]
                    and after["input_tokens"] == before["input_tokens"]):
                # answered, so billed, but its usage reported no input tokens
                self.unbilled_usd += next_usd
            return answers


# --- the factory ---------------------------------------------------------------------

def _cache_path() -> Path:
    raw = os.environ.get(CACHE_ENV, "").strip()
    return Path(raw) if raw else REPO_ROOT / DEFAULT_CACHE


def get(mode: str = "") -> Jev:
    """The judge for `mode`: the argument, else `AUTO_APPLY_JEV_MODE`, else
    "typesafe". "fake" never needs a key. "replay" only reads the cache file
    `AUTO_APPLY_JEV_CACHE` names (default `tests/fixtures/jev_cache/cache.json`
    under the repo), and refuses a missing recording. Explicit recording uses
    `ReplayJev(inner, path)` so a replay cannot silently buy calls or cache fake
    judgments when credentials are absent.
    "typesafe" raises `JevUnavailable` when `TYPESAFE_API_KEY` is unset."""
    mode = (mode or os.environ.get(MODE_ENV, "") or "typesafe").strip().lower()
    if mode == "fake":
        return FakeJev()
    if mode == "replay":
        return ReplayJev(None, _cache_path())
    if mode == "typesafe":
        return TypeSafeJev()
    raise ValueError(f"unknown Jev mode {mode!r}; expected one of {', '.join(MODES)}")
