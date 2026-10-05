"""The judge's test doubles: `NoisyJev` and `DryRun`.

Neither is a production judge: `jev.get` builds only the live client, the
fake and the replay cache, and `apply_run drain` refuses anything but the live
judge. They have their own module so `jev` holds only what production runs.
Their users are the tests, `tests/apply_harness.py`, `scripts/apply_matrix.py`
and `scripts/jev_score_calibrate.py`; `scripts/` cannot import from `tests/`,
so the module sits in `local/`.

- `NoisyJev(inner, seed)` bends another judge's answers the way a real misread
  would (a neighbouring page state, lower confidences, two buttons' roles
  exchanged, a field mapping dropped), the same way for the same request:
  its draws key on `jev.ReplayJev.key_for`. The flow matrix and the invariant
  harness run on it.
- `DryRun(inner)` answers through the fake and counts each request as a
  simulated one of its estimated size, so a `jev.SpendCap` over it and a
  recording's dry-run estimate work as they would live.
"""
from __future__ import annotations

import hashlib
from typing import Any

from jev import (PAGE_KIND_NOULS, READ_NOULS, Answer, FakeJev, Jev, ReplayJev, count_usage,
                 request_size)


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
# the answers a drop may remove: a field's mapping, and a validation
# message's field (`apply_judge.error_questions`)
_DROPPED_PREFIXES = ("field_", "error_")
# A validation message's field is misread like a page state: with
# chance `swap_p` it points at another option (another field, or `none`),
# the true one second; a share of those flips (`error_sure_p`) lands at 0.70
# to 0.90, above the field floor (`apply_judge.FIELD_MAP_MIN_CONF`), as a
# confident wrong mapping the run acts on; the rest at 0.30 to 0.60.
_ERROR_PREFIX = "error_"
_SURE_ERROR_CONF = (0.70, 0.90)


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
      `field_{n}_option`, `field_{n}_pick`) and each validation message's
      field (`error_{i}_field`) is dropped with this chance, as a
      misread that leaves the box or the message without a mapping.
    - A validation message's field (`error_{i}_field`) is misread as
      a page state is: with chance `swap_p` it names another option
      (another field, or `none`), the true answer second; with chance
      `error_sure_p` (default 0.5) such a flip is read at 0.70 to 0.90,
      above the field floor, a confident wrong mapping the run acts on, and
      otherwise at 0.30 to 0.60; an answer not flipped has its confidence
      scaled as every choice's is.
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
                 noul_p: float | None = None, coherent_p: float = 0.5,
                 error_sure_p: float = 0.5):
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
        self.error_sure_p = float(error_sure_p)

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
            if qid.startswith(_DROPPED_PREFIXES) and rng.random() < self.drop_p:
                continue
            if a.kind == "noul" and qid in READ_NOULS:
                out[qid] = self._read_noul(qid, a, rng, truth, misread)
            elif a.kind == "choice" and qid.startswith(_ERROR_PREFIX):
                out[qid] = self._error_field(a, rng)
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

    def _error_field(self, a: Answer, rng) -> Answer:
        """A validation message's field: with chance `swap_p` another
        option (a field, or `none`), the true answer second: with chance
        `error_sure_p` at 0.70 to 0.90 (above the field floor: a confident
        wrong mapping), else at 0.30 to 0.60; else the answer
        with its confidence scaled."""
        names = list(a.probabilities) or [str(a.choice)]
        truth = str(a.choice)
        swap, pick, conf_draw = rng.random(), rng.random(), rng.random()
        others = [n for n in names if n != truth]
        if others and swap < self.swap_p:
            winner = others[int(pick * len(others)) % len(others)]
            span = _SURE_ERROR_CONF if rng.random() < self.error_sure_p else _SWAPPED_CONF
            conf = round(_between(span, conf_draw), 4)
            return Answer(kind="choice", choice=winner,
                          probabilities=_spread(names, winner, conf, truth), confidence=conf)
        return self._scaled(a, rng)

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


class DryRun:
    """A stand-in for the live judge in a recording's dry run: `inner`
    (the fake by default) answers, and each request counts as one simulated
    request of its estimated size (`request_size`, the whole request,
    `count_usage(..., simulated=True)`), so a `SpendCap` over it and a dry
    run's estimate work as they would live. The live counters (`usage()`,
    `total_usage()`) never see it. No key, no network. Never a production
    judge."""

    simulated = True

    def __init__(self, inner: Jev | None = None):
        self.inner = inner if inner is not None else FakeJev()
        self.requests = 0
        self.tokens = 0

    def judge(self, state: Any, questions: dict[str, dict]) -> dict[str, Answer]:
        tokens = request_size(state, questions)[1]
        answers = self.inner.judge(state, questions)
        self.requests += 1
        self.tokens += tokens
        count_usage(tokens, simulated=True)
        return answers
