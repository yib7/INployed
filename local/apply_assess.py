"""The auto-apply difficulty check (cycle 19, DF-1 to DF-6): a 1-10 score per
queued job, so the user knows which jobs to leave to the drain.

DF-3's score is code: a base by application system plus fixed steps for what
the first application page asks, rounded half up and clamped to 1-10. Easy
Apply, a closed or dead posting and a payment page are 10 at once. Jev reads
the page; the counting and the arithmetic stay here, with the constants below.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# --- DF-3: the score -------------------------------------------------------------------

# The base by application system (the `apply_queue.infer_ats` names, plus
# "bamboohr"); a system missing here scores UNKNOWN_BASE.
SYSTEM_BASE = {
    "greenhouse": 2, "lever": 2, "ashby": 2,
    "workable": 3, "smartrecruiters": 3, "jobvite": 3, "bamboohr": 3,
    "workday": 6, "icims": 6,
    "taleo": 7, "successfactors": 7, "oracle": 7,
}
UNKNOWN_BASE = 4
PER_UNANSWERED = 1.5        # each required question the answers cannot fill
UNANSWERED_CAP = 5
PER_ESSAY = 0.5             # each required essay the run would draft
ESSAY_CAP = 2
SENSITIVE = 3               # a required sensitive field (always the user's to type)
CAPTCHA = 2                 # a CAPTCHA or bot check
ACCOUNT_WALL = 1            # an account wall with no saved account
HISTORY_EASIER = -1         # past runs on the system reached the end twice, no park
HISTORY_HARDER = 1          # past runs on the system parked twice
HISTORY_MIN = 2
SCORE_MIN, SCORE_MAX = 1, 10
STOP_SCORE = 10

BANDS = ((1, 3, "Queue it"), (4, 6, "May need an answer or two"), (7, 10, "Do it yourself"))

# The pages the score stops at, at STOP_SCORE, each with its one reason.
STOP_REASONS = {
    "easy_apply": "Easy Apply: the run leaves Easy Apply jobs to you",
    "closed": "The posting is closed",
    "dead": "The application is a dead end: an error page, or no Apply entry to follow",
    "payment": "The application asks for a payment",
}

SYSTEM_NAMES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "workable": "Workable", "smartrecruiters": "SmartRecruiters", "jobvite": "Jobvite",
    "bamboohr": "BambooHR", "workday": "Workday", "icims": "iCIMS", "taleo": "Taleo",
    "successfactors": "SuccessFactors", "oracle": "Oracle",
}


def band_for(value: int) -> str:
    """The band a score falls in: 1-3 "Queue it", 4-6 "May need an answer or
    two", 7-10 "Do it yourself"."""
    for low, high, label in BANDS:
        if low <= value <= high:
            return label
    raise ValueError(f"a difficulty score runs {SCORE_MIN} to {SCORE_MAX}; got {value!r}")


def _half_up(total: float) -> int:
    return int(math.floor(total + 0.5))


def _step(value: float) -> str:
    return f"{value:+g}"


def _capped(count: int, per: float, cap: float) -> tuple[float, str]:
    raw = count * per
    if raw > cap:
        return float(cap), f"{_step(cap)}, the most this step adds"
    return raw, _step(raw)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def score(*, system: str = "", unanswered: int = 0, essays: int = 0,
          sensitive: bool = False, captcha: bool = False, account_wall: bool = False,
          past_submits: int = 0, past_parks: int = 0, stop: str = "") -> dict:
    """DF-3: {"score", "band", "reasons"} for what the check read.

    `system` is the application system (a SYSTEM_BASE key; anything else is
    unknown). `unanswered` counts the required questions the user's confirmed
    answers cannot fill, `essays` the required essays the run would draft.
    `past_submits` and `past_parks` count the user's past runs on that system
    that reached the end and that parked (`past_runs`): two parks add
    HISTORY_HARDER, and two runs that reached the end with no park add
    HISTORY_EASIER. `stop` (a STOP_REASONS key) scores STOP_SCORE at once with
    its one reason; an unknown stop raises ValueError."""
    if stop:
        if stop not in STOP_REASONS:
            raise ValueError(f"unknown stop {stop!r}; expected one of {', '.join(STOP_REASONS)}")
        return {"score": STOP_SCORE, "band": band_for(STOP_SCORE),
                "reasons": [STOP_REASONS[stop]]}
    key = str(system or "").strip().lower()
    name = SYSTEM_NAMES.get(key)
    base = SYSTEM_BASE.get(key, UNKNOWN_BASE)
    total = float(base)
    reasons = [f"Application system: {name} (base {base})" if name else
               f"Application system: not one the check knows (base {base})"]
    if unanswered > 0:
        add, shown = _capped(unanswered, PER_UNANSWERED, UNANSWERED_CAP)
        total += add
        reasons.append(f"{_plural(unanswered, 'required question', 'required questions')} "
                       f"your answers cannot fill ({shown})")
    if essays > 0:
        add, shown = _capped(essays, PER_ESSAY, ESSAY_CAP)
        total += add
        reasons.append(f"{_plural(essays, 'required essay', 'required essays')} "
                       f"the run would draft ({shown})")
    if sensitive:
        total += SENSITIVE
        reasons.append(f"A required sensitive field, always yours to type ({_step(SENSITIVE)})")
    if captcha:
        total += CAPTCHA
        reasons.append(f"A CAPTCHA or bot check ({_step(CAPTCHA)})")
    if account_wall:
        total += ACCOUNT_WALL
        reasons.append(f"An account wall with no saved account ({_step(ACCOUNT_WALL)})")
    where = name or "this system"
    if past_parks >= HISTORY_MIN:
        total += HISTORY_HARDER
        reasons.append(f"Past runs on {where} parked at least twice ({_step(HISTORY_HARDER)})")
    elif past_submits >= HISTORY_MIN and past_parks == 0:
        total += HISTORY_EASIER
        reasons.append(f"Past runs on {where} reached the end at least twice with no park "
                       f"({_step(HISTORY_EASIER)})")
    value = min(SCORE_MAX, max(SCORE_MIN, _half_up(total)))
    return {"score": value, "band": band_for(value), "reasons": reasons}
