"""Jev (TypeSafe System One) client wrapper for the auto-apply loop.

Every judgment the apply loop makes goes through one `judge(state, questions)`
call that returns an `Answer` per question id. Three implementations share that
shape:

- `TypeSafeJev` wraps `typesafe_sdk.TypeSafeClient` with the model pinned to
  `jev-1.13.0` (thresholds are tuned per version; the docs say pin) and keeps a
  process-wide usage counter (`usage()`, `reset_usage()`).
- `FakeJev` is deterministic and key-free: word overlap between the question
  and the state. It drives the loop in tests and dry runs; its rules are in the
  class docstring so fixtures can be written against them.
- `ReplayJev(inner, cache_path)` replays a recorded answer for a request it has
  seen and records through `inner` on a miss, so a committed cache can stand in
  for the live model.

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
import os
import re
from collections.abc import Mapping
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


def usage() -> dict:
    """{"requests", "input_tokens", "usd"} for every live request this process
    made. The fake and a replay hit never count."""
    tokens = _USAGE["input_tokens"]
    return {"requests": _USAGE["requests"], "input_tokens": tokens,
            "usd": tokens / 1_000_000 * PRICE_USD_PER_MTOK}


def reset_usage() -> None:
    _USAGE["requests"] = 0
    _USAGE["input_tokens"] = 0


# --- the live client --------------------------------------------------------------

class TypeSafeJev:
    """The live judge over `typesafe_sdk.TypeSafeClient`.

    The key comes from the `api_key` argument, else `TYPESAFE_API_KEY` in the
    environment (`.env` reaches it through the dashboard's and the runner's
    `load_dotenv`). Construction raises `JevUnavailable` when the key or the SDK
    is missing, so a run refuses to start before it can park every job. No
    request is made until `judge()`.
    """

    def __init__(self, api_key: str | None = None, model: str = MODEL):
        key = (api_key or os.environ.get(KEY_ENV, "")).strip()
        if not key:
            raise JevUnavailable(
                f"No TypeSafe API key. Create one at {CONSOLE_KEYS_URL}, then add the "
                f"row `{KEY_ENV}=<your key>` to .env (or paste it into Settings > "
                "Auto-apply > TypeSafe API key) and restart the dashboard.")
        try:
            import typesafe_sdk
        except ImportError as exc:
            raise JevUnavailable(
                "The typesafe_sdk package is not installed: run "
                "`pip install typesafe-sdk` (it is in requirements.txt).") from exc
        self.model = model
        self.last_model: str | None = None
        self._client = typesafe_sdk.TypeSafeClient(api_key=key, model=model)

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        response = self._client.system_one(state, questions, model=self.model)
        tokens = int(getattr(response.usage, "input_tokens", 0) or 0)
        _USAGE["requests"] += 1
        _USAGE["input_tokens"] += tokens
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


class FakeJev:
    """A deterministic, key-free judge for tests and dry runs. Never used for a
    real application on purpose: it only matches words.

    Rules (fixtures are written against these; change them and the fixtures):

    - Text is tokenised into lowercase word sets (`[a-z0-9]+`, so `leave_blank`
      is the two words `leave` and `blank`). Instructions may be a string, an
      object or an array; every string inside counts.
    - The state text is the slice(s) the instructions name with backticked
      paths (`sheet.work_authorization`, `field.options[0]`), serialised as
      JSON. When no named path resolves, it is the whole state as JSON, cut to
      its first 20,000 characters. The path names themselves are removed from
      the instruction text before tokenising.
    - Choice: each option scores the number of its distinct words (option name
      plus its description, stopwords dropped) that appear in the instruction
      words plus the state words. Highest wins; a tie keeps the earlier option.
      When every overlap is zero the winner is, in order of presence, `none`,
      `other`, `leave_blank`, `no_match`, else the first option.
      `probabilities` puts 1.0 on the winner and 0.0 elsewhere; `confidence`
      is 1.0.
    - Noul: 0.9 when at least two distinct instruction content words (four or
      more letters, stopwords excluded) appear in the state text, else 0.1.
      `probabilities` is empty and `confidence` is None, as in the API.
    - Score: the level whose text overlaps the instruction plus state words
      most (a tie keeps the lower level); `score` is that level's index as a
      float, `probabilities` puts 1.0 on it (keyed by the index as a string)
      and `confidence` is 1.0.
    """

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            kind = str(q.get("type", ""))
            instr_text, state_text = _fake_texts(state, q.get("instructions", ""))
            context = _words(instr_text) | _words(state_text)
            if kind == "noul":
                out[qid] = self._noul(instr_text, state_text)
            elif kind == "choice":
                out[qid] = self._choice(q.get("criteria") or {}, context)
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
    def _choice(criteria: Mapping[str, Any], context: set[str]) -> Answer:
        names = list(criteria)
        if not names:
            raise ValueError("a choice question needs at least one option")
        scores = {name: len(_words(f"{name} {_flatten(desc)}") & context)
                  for name, desc in criteria.items()}
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


def _flatten(value: Any) -> str:
    """Every string inside a str / object / array, space-joined; object keys are
    labels and are left out."""
    if value is None:
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


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _fake_texts(state: Any, instructions: Any) -> tuple[str, str]:
    """(instruction text with the backticked paths removed, the state text the
    instructions point at)."""
    raw = _flatten(instructions)
    slices = []
    for path in _PATH_RE.findall(raw):
        ok, value = _resolve(state, path)
        if ok:
            slices.append(_as_text(value))
    instr_text = _PATH_RE.sub(" ", raw)
    if slices:
        return instr_text, " ".join(slices)
    return instr_text, _as_text(state)[:_WHOLE_STATE_CAP]


# --- the replay cache ---------------------------------------------------------------

class ReplayJev:
    """Replays a recorded answer for a request it has seen; records through
    `inner` on a miss.

    The key is the sha256 of the canonical JSON of `{"state", "questions"}`
    (sorted keys, no whitespace), so key order in the state does not matter and
    any change to a question does. The cache is one JSON object
    `{key: {question_id: answer}}` written atomically after every miss.
    """

    def __init__(self, inner: Jev, cache_path: Path):
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.hits = 0
        self.misses = 0
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
            return {qid: Answer.from_dict(raw) for qid, raw in hit.items()}
        self.misses += 1
        answers = self.inner.judge(state, questions)
        cache[key] = {qid: a.to_dict() for qid, a in answers.items()}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.cache_path, cache)
        return answers


# --- the factory ---------------------------------------------------------------------

def _cache_path() -> Path:
    raw = os.environ.get(CACHE_ENV, "").strip()
    return Path(raw) if raw else REPO_ROOT / DEFAULT_CACHE


def get(mode: str = "") -> Jev:
    """The judge for `mode`: the argument, else `AUTO_APPLY_JEV_MODE`, else
    "typesafe". "fake" never needs a key. "replay" wraps the live client when a
    key is set and the fake otherwise, over the cache file `AUTO_APPLY_JEV_CACHE`
    names (default `tests/fixtures/jev_cache/cache.json` under the repo).
    "typesafe" raises `JevUnavailable` when `TYPESAFE_API_KEY` is unset."""
    mode = (mode or os.environ.get(MODE_ENV, "") or "typesafe").strip().lower()
    if mode == "fake":
        return FakeJev()
    if mode == "replay":
        inner: Jev = TypeSafeJev() if os.environ.get(KEY_ENV, "").strip() else FakeJev()
        return ReplayJev(inner, _cache_path())
    if mode == "typesafe":
        return TypeSafeJev()
    raise ValueError(f"unknown Jev mode {mode!r}; expected one of {', '.join(MODES)}")
