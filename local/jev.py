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
- `NoisyJev(inner, seed)` bends another judge's answers the way a real misread
  would (a neighbouring page state, lower confidences, two buttons' roles
  exchanged, a field mapping dropped), the same way for the same request. The
  flow matrix and the invariant harness run on it; nothing in production does.

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


# --- the noisy judge (tests and diagnostics only) -------------------------------------

# The misreads the live judge could plausibly make, per page state. A swap goes
# to one of these, never to an unrelated kind (a job posting is never read as a
# payment page). A form or a review read as a confirmation (`CONFIRM_MISREADS`)
# is drawn apart, at its own chance and a confidence that can pass the gates.
PAGE_STATE_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "job_posting": ("other", "application_form"),
    "application_form": ("signup_form", "review_page", "confirmation"),
    "review_page": ("application_form", "confirmation"),
    "signup_form": ("login_wall", "application_form"),
    "login_wall": ("signup_form",),
    "code_gate": ("login_wall",),
    "confirmation": ("other",),
    "other": ("job_posting", "confirmation"),
    "captcha_or_bot_check": ("other",),
    "error_or_dead": ("other",),
    "payment_request": ("other",),
}
# Button roles a misread can exchange between two buttons of one page. `submit`
# is the final role and never moves; `back` and `advance` never trade places
# (no reading of a wizard's footer takes Back for Continue).
BUTTON_ROLE_NEIGHBOURS: frozenset[frozenset[str]] = frozenset(
    frozenset(pair) for pair in (("advance", "apply_entry"), ("advance", "other"),
                                 ("apply_entry", "other"), ("upload", "other"),
                                 ("back", "other")))
# The page read's Nouls (`apply_judge.read_questions`), by the page kind each
# speaks for; `page_already_applied` speaks for none (its own park reason).
# `NoisyJev` misreads them with the page state: a misread page state takes its
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
_SWAPPED_CONF = (0.30, 0.60)    # the confidence a swapped page state is read at
# A flipped read Noul lands on the wrong side of 0.5 with room to spare: a yes
# read from 0.10 to 0.40, a no from 0.60 to 0.90. A coherent misread's Nouls:
# the misread kind's yes from 0.60 to 0.85, the true kind's no from 0.15 to 0.40.
_FLIPPED_YES = (0.10, 0.40)
_FLIPPED_NO = (0.60, 0.90)
_COHERENT_YES = (0.60, 0.85)
_COHERENT_NO = (0.15, 0.40)
# A form or a review page read as a confirmation, before the submit or after
# it (a validation page, a form that did not change): drawn apart from the
# swaps, at a confidence from 0.40 to 0.80, above the page-state floor and
# often above the confirmation floor, the read that must never end a job
# `submitted` without a confirmation on the page.
CONFIRM_MISREADS = frozenset(("application_form", "review_page"))
_CONFIRM_MISREAD_CONF = (0.40, 0.80)
_DROPPED_PREFIX = "field_"      # the answers a drop may remove


class NoisyJev:
    """A judge that misreads the way a real one might, for the flow matrix
    and the invariant harness. Never a production judge: `jev.get` has no
    mode for it and `apply_run drain` refuses anything but the live judge.

    It wraps `inner` (a `FakeJev` or a `ReplayJev`) and bends its answers:

    - `swap_p` (default 0.15): per request, the page state is read as a
      plausible neighbour (`PAGE_STATE_NEIGHBOURS`) at a confidence between
      0.30 and 0.60, with the true state second in the distribution. The same
      chance, drawn apart, exchanges the roles of two buttons whose roles are
      neighbours (`BUTTON_ROLE_NEIGHBOURS`); a `submit` role never moves.
    - `conf_scale` (default 0.75): every choice and score confidence is
      multiplied by a factor drawn from [conf_scale, 1.0]; the winner keeps
      that much probability and the rest spreads over the other options. A
      Noul (a verification, a flag) is left alone: it carries no confidence.
    - `drop_p` (default 0.05): each field answer (`field_{n}_source`,
      `field_{n}_option`, `field_{n}_pick`) is dropped with this chance, as a
      misread that leaves the box without a mapping.
    - `confirm_p` (default a third of `swap_p`, 0.05): a form or a review
      page not swapped otherwise is read as a confirmation
      (`CONFIRM_MISREADS`) at a confidence from 0.40 to 0.80, the true state
      second.
    - The page read's Nouls (`READ_NOULS`): when the page state was misread
      (a swap or a confirmation misread), with chance `coherent_p` (default
      0.5, drawn per Noul) the misread kind's Nouls read yes (0.60 to 0.85)
      and the true kind's read no (0.15 to 0.40), as a judge that took the
      page for the other kind would answer; otherwise each is flipped with
      chance `noul_p` (default `swap_p`) to the wrong side of 0.5 (a yes read
      from 0.10 to 0.40, a no from 0.60 to 0.90), and one not flipped is
      pulled toward 0.5 by a factor drawn from [conf_scale, 1.0]. The other
      Nouls (a verification, a flag, a button's `sends`) are left alone.

    The live judge answers the same request the same way, so the noise is a
    function of (`seed`, the request): the same state and questions get the
    same answers every time, and a re-read of a changed page gets fresh noise.
    The defaults put about one page in seven on a neighbour's reading, keep
    every confident answer above 0.75 of its value, and leave most pages'
    field mappings whole: enough to find the steps that trust one read, while
    a loop that checks what it reads can still finish."""

    def __init__(self, inner: Jev, seed: int, *, swap_p: float = 0.15,
                 conf_scale: float = 0.75, drop_p: float = 0.05,
                 role_p: float | None = None, confirm_p: float | None = None,
                 noul_p: float | None = None, coherent_p: float = 0.5):
        if not 0.0 < conf_scale <= 1.0:
            raise ValueError("conf_scale must be in (0, 1]")
        self.inner = inner
        self.seed = int(seed)
        self.swap_p = float(swap_p)
        self.conf_scale = float(conf_scale)
        self.drop_p = float(drop_p)
        self.role_p = self.swap_p if role_p is None else float(role_p)
        self.confirm_p = self.swap_p / 3 if confirm_p is None else float(confirm_p)
        self.noul_p = self.swap_p if noul_p is None else float(noul_p)
        self.coherent_p = float(coherent_p)

    def _rng(self, request_key: str, part: str):
        import random
        digest = hashlib.sha256(f"{self.seed}:{request_key}:{part}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        answers = dict(self.inner.judge(state, questions))
        key = ReplayJev.key_for(state, questions)
        out: dict[str, Answer] = {}
        # the page state first: a misread takes the read's Nouls along
        truth = misread = ""
        page = answers.get("page_state")
        if page is not None and page.kind == "choice":
            out["page_state"] = self._page_state(page, self._rng(key, "page_state"))
            truth = str(page.choice or "other")
            if out["page_state"].choice != truth:
                misread = str(out["page_state"].choice)
        for qid in sorted(answers):
            if qid in out:
                continue
            a = answers[qid]
            rng = self._rng(key, qid)
            if qid.startswith(_DROPPED_PREFIX) and rng.random() < self.drop_p:
                continue
            if a.kind == "noul" and qid in READ_NOULS:
                out[qid] = self._read_noul(qid, a, rng, truth, misread)
            elif a.kind in ("choice", "score"):
                out[qid] = self._scaled(a, rng)
            else:
                out[qid] = a
        self._exchange_roles(out, self._rng(key, "roles"))
        return {qid: out[qid] for qid in answers if qid in out}

    def _read_noul(self, qid: str, a: Answer, rng, truth: str, misread: str) -> Answer:
        """A page-read Noul as a misreading judge answers it (see the class
        docstring): along with a misread page state, flipped on its own, or
        pulled toward 0.5."""
        value = float(a.noul if a.noul is not None else 0.5)
        coherent, flip, draw, scale = rng.random(), rng.random(), rng.random(), rng.random()
        if misread and coherent < self.coherent_p:
            if qid in PAGE_KIND_NOULS.get(misread, ()):
                return Answer(kind="noul", noul=round(_between(_COHERENT_YES, draw), 4))
            if qid in PAGE_KIND_NOULS.get(truth, ()):
                return Answer(kind="noul", noul=round(_between(_COHERENT_NO, draw), 4))
        if flip < self.noul_p:
            span = _FLIPPED_YES if value >= 0.5 else _FLIPPED_NO
            return Answer(kind="noul", noul=round(_between(span, draw), 4))
        factor = self.conf_scale + (1.0 - self.conf_scale) * scale
        return Answer(kind="noul", noul=round(0.5 + (value - 0.5) * factor, 4))

    def _factor(self, rng) -> float:
        return rng.uniform(self.conf_scale, 1.0)

    def _page_state(self, a: Answer, rng) -> Answer:
        names = list(a.probabilities) or [a.choice or "other"]
        truth = str(a.choice or "other")
        swap_draw, pick_draw, conf_draw = rng.random(), rng.random(), rng.random()
        neighbours = tuple(n for n in PAGE_STATE_NEIGHBOURS.get(truth, ())
                           if not (truth in CONFIRM_MISREADS and n == "confirmation"))
        if neighbours and swap_draw < self.swap_p:
            winner = neighbours[int(pick_draw * len(neighbours)) % len(neighbours)]
            low, high = _SWAPPED_CONF
            conf = round(low + (high - low) * conf_draw, 4)
            second = truth
        else:
            winner = truth
            conf = round(float(a.confidence if a.confidence is not None else 1.0)
                         * self._factor(rng), 4)
            second = neighbours[0] if neighbours else ""
            if truth in CONFIRM_MISREADS and rng.random() < self.confirm_p:
                winner, second = "confirmation", truth
                low, high = _CONFIRM_MISREAD_CONF
                conf = round(low + (high - low) * rng.random(), 4)
        return Answer(kind="choice", choice=winner,
                      probabilities=_spread(names, winner, conf, second),
                      confidence=conf)

    def _scaled(self, a: Answer, rng) -> Answer:
        conf = round(float(a.confidence if a.confidence is not None else 1.0)
                     * self._factor(rng), 4)
        if a.kind == "score":
            return Answer(kind="score", score=a.score, probabilities=dict(a.probabilities),
                          confidence=conf)
        names = list(a.probabilities) or [a.choice]
        return Answer(kind="choice", choice=a.choice,
                      probabilities=_spread(names, str(a.choice), conf, ""),
                      confidence=conf)

    def _exchange_roles(self, out: dict[str, Answer], rng) -> None:
        """Exchange the roles of one pair of buttons whose roles are
        neighbours, with chance `role_p`; the pair is drawn from the page's
        eligible pairs."""
        if rng.random() >= self.role_p:
            return
        roles = {qid: a for qid, a in out.items()
                 if qid.startswith("button_") and qid.endswith("_role")
                 and a.kind == "choice" and a.choice and a.choice != "submit"}
        ids = sorted(roles)
        pairs = [(x, y) for i, x in enumerate(ids) for y in ids[i + 1:]
                 if frozenset((roles[x].choice, roles[y].choice)) in BUTTON_ROLE_NEIGHBOURS]
        if not pairs:
            return
        x, y = pairs[int(rng.random() * len(pairs)) % len(pairs)]
        ax, ay = roles[x], roles[y]
        out[x] = Answer(kind="choice", choice=ay.choice,
                        probabilities=_spread(list(ax.probabilities) or [ay.choice],
                                              str(ay.choice), float(ax.confidence or 0.0), ""),
                        confidence=ax.confidence)
        out[y] = Answer(kind="choice", choice=ax.choice,
                        probabilities=_spread(list(ay.probabilities) or [ax.choice],
                                              str(ax.choice), float(ay.confidence or 0.0), ""),
                        confidence=ay.confidence)


def _between(span: tuple[float, float], draw: float) -> float:
    low, high = span
    return low + (high - low) * draw


def _spread(names: list[str], winner: str, conf: float, second: str) -> dict[str, float]:
    """A distribution led by `winner` at `conf` or more: `second` (when named)
    takes the most of the rest short of the winner, the other options share
    what is left, each at most 0.9 of the winner's share, and whatever no
    option may take goes to the winner. The winner is always the most
    probable option and the probabilities sum to one."""
    names = list(dict.fromkeys([*names, winner] + ([second] if second else [])))
    probs = {n: 0.0 for n in names}
    probs[winner] = conf
    rest = max(0.0, 1.0 - conf)
    others = [n for n in names if n != winner]
    if second and second != winner:
        probs[second] = min(rest, conf * 0.9)
        rest -= probs[second]
        others = [n for n in others if n != second]
    if others:
        share = min(rest / len(others), conf * 0.9)
        for n in others:
            probs[n] = share
        rest -= share * len(others)
    probs[winner] += rest
    return {n: round(p, 4) for n, p in probs.items()}


# --- the replay cache ---------------------------------------------------------------

class ReplayJev:
    """Replays a recorded answer for a request it has seen; records through
    `inner` on a miss. With `inner=None`, a miss raises `JevUnavailable` and
    the cache remains unchanged.

    The key is the sha256 of the canonical JSON of `{"state", "questions"}`
    (sorted keys, no whitespace), so key order in the state does not matter and
    any change to a question does. The cache is one JSON object
    `{key: {question_id: answer}}` written atomically after every miss.
    """

    def __init__(self, inner: Jev | None, cache_path: Path):
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
        if self.inner is None:
            raise JevUnavailable("Jev replay cache miss; record this fixture explicitly first")
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
