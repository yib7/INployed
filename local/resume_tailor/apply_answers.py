"""The master answer store: the user's saved answers to screening questions.

Version 2 types every answer, so a form gets the answer you picked,
or a blank. The file (repo-root `apply_answers.json`) is

    {"version": 2,
     "answers": [{"id", "question", "type", "answer", "note", "confirmed", "status"}, ...],
     "review": [{"id", "question", "before", "after"}, ...]}

`type` is yes_no, number, choice or text; an `answer` of "" is not set; `note`
holds the words around a yes/no or number answer; the run uses only confirmed
answers and reads them through `fact_value`, the one reader. `BUILTINS` names the
standard questions with their types and options; they seed from
`apply_config.DEFAULTS`, unconfirmed. A version 1 file (no `version` key, free
text answers) migrates in memory on load (`migrate_v1`) and the next save writes
version 2; `review` lists what the migration converted until the user dismisses
it. A file that exists but cannot be read raises `AnswerStoreError`: nothing
falls back to defaults silently.

The file is personal, so it is git-ignored; absent, `load()` returns the seeded
defaults in memory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

from . import apply_config
from .answer_tables import (
    CHOICE_ALIASES, COUNTRIES, DECLINE, DISABILITY_OPTIONS, GENDER_OPTIONS, RACE_OPTIONS,
    STATE_NAMES, US_COUNTRY, US_STATES, VETERAN_OPTIONS,
)

__all__ = [
    "AnswerStoreError", "BOOL_IDS", "BUILTINS", "Builtin", "CUSTOM_TYPES",
    "NOTE_MAX", "NUMBER_MAX", "STATE_TEXT_MAX", "STATUSES", "STORE_PATH", "TEXT_MAX",
    "TYPES", "US_STATES", "VERSION", "YES_NO", "fact_value",
    "find_collision", "load", "load_store", "load_with_defaults", "match_option",
    "migrate_from_apply_config", "migrate_v1", "new_id", "restore_bytes", "save",
    "seed_defaults", "validate", "warnings", "with_missing_builtins", "yes_no",
]

PKG_DIR = Path(__file__).resolve().parent          # local/resume_tailor
REPO_ROOT = PKG_DIR.parent.parent                  # scrape_data
STORE_PATH = REPO_ROOT / "apply_answers.json"

VERSION = 2

# A v1 entry's `kind` field is kept on load and ignored.
# Every saved entry is active; a v1 "needs-review" row migrates to active, unconfirmed.
STATUSES = ("active",)

TYPES = ("yes_no", "number", "choice", "text")  # every type the store holds
CUSTOM_TYPES = ("text", "yes_no", "number")      # what "Add answer" offers
YES_NO = ("Yes", "No")
TEXT_MAX = 1000
NOTE_MAX = 300
STATE_TEXT_MAX = 60                              # a state or province outside the US
NUMBER_MAX = 60
_NUMBER_RE = re.compile(r"^\d{1,2}(\.5)?$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


class Builtin(NamedTuple):
    question: str
    type: str
    options: Tuple[str, ...] = ()


# The standard questions, in the order the editor and the seed list them.
BUILTINS: Dict[str, Builtin] = {
    "work_authorized": Builtin("Are you legally authorized to work in the US?", "yes_no"),
    "requires_sponsorship": Builtin(
        "Will you now or in the future require visa sponsorship?", "yes_no"),
    "willing_to_relocate": Builtin("Are you willing to relocate?", "yes_no"),
    "onsite_ok": Builtin("Are you willing to work on-site (in the office)?", "yes_no"),
    "years_experience": Builtin("How many years of relevant experience do you have?",
                                "number"),
    "authorization_statement": Builtin("Work-authorization statement (free text).", "text"),
    "gender": Builtin("Gender (EEO self-identification).", "choice", GENDER_OPTIONS),
    "race_ethnicity": Builtin("Race / ethnicity (EEO self-identification).", "choice",
                              RACE_OPTIONS),
    "veteran_status": Builtin("Veteran status (EEO self-identification).", "choice",
                              VETERAN_OPTIONS),
    "disability_status": Builtin("Disability status (EEO self-identification).", "choice",
                                 DISABILITY_OPTIONS),
    "how_did_you_hear": Builtin("How did you hear about us?", "text"),
    "address_street": Builtin("Street address (line 1).", "text"),
    "address_city": Builtin("City.", "text"),
    # a choice of STATE_NAMES when the country is the United States (or not set),
    # else text up to STATE_TEXT_MAX characters
    "address_state": Builtin("State / province.", "choice", STATE_NAMES),
    # text; a US ZIP code (_ZIP_RE) when the country is the United States (or not set)
    "address_zip": Builtin("ZIP / postal code.", "text"),
    "address_country": Builtin("Country.", "choice", COUNTRIES),
}

# The yes/no ids: the sheet and the fact catalog show them as Yes or No.
BOOL_IDS = frozenset(k for k, b in BUILTINS.items() if b.type == "yes_no")
_BUILTIN_QUESTIONS = frozenset(b.question for b in BUILTINS.values())


class AnswerStoreError(Exception):
    """The answer file exists but cannot be used (unreadable, not JSON, wrong shape).
    Deliberately not a ValueError, so no `except ValueError` swallows it."""

    def __init__(self, path: Union[Path, str], reason: str):
        super().__init__(path, reason)
        self.path = path
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


# --- reading an answer --------------------------------------------------------------------

_YES_WORDS = {"yes", "true", "1"}
_NO_WORDS = {"no", "false", "0"}
_FIRST_WORD = re.compile(r"[a-z0-9]+")


def yes_no(value: Any) -> str:
    """"Yes" or "No" when a yes/no answer opens with one ("true", "Yes, I am a
    US citizen", "no."), else "". Used to migrate version 1 text; the run reads
    answers through `fact_value`."""
    m = _FIRST_WORD.match(str(value or "").strip().lower())
    word = m.group(0) if m else ""
    if word in _YES_WORDS:
        return "Yes"
    if word in _NO_WORDS:
        return "No"
    return ""


def match_option(value: Any, options: Tuple[str, ...]) -> Optional[str]:
    """The option `value` names once stripped, case aside ("Yes " and "yes" are
    "Yes"), or None when it names none or is blank. The one normalizer the run
    (`fact_value`) and the editor's option boxes share."""
    text = str(value or "").strip().casefold()
    if not text:
        return None
    return next((o for o in options if o.casefold() == text), None)


def _type_ok(eid: str, etype: Any) -> bool:
    builtin = BUILTINS.get(eid)
    return etype == builtin.type if builtin is not None else etype in CUSTOM_TYPES


def _answer_problem(eid: str, etype: str, answer: str, us: Optional[bool]) -> Optional[str]:
    """Why a set answer does not fit its type, or None. `us` says whether the
    address follows the US rules (None: not known, only the general limits apply)."""
    builtin = BUILTINS.get(eid)
    if etype == "yes_no":
        if answer not in YES_NO:
            return "a yes/no answer must be Yes, No or not set"
    elif etype == "number":
        if not _NUMBER_RE.match(answer) or float(answer) > NUMBER_MAX:
            return ("a number must be a whole or half number from 0 to 60, such as 3 or 2.5 "
                    "(the words around it go in the note)")
    elif etype == "choice":
        if eid == "address_state":
            if us is True and answer not in STATE_NAMES:
                return ("pick a US state from the list by its full name, such as "
                        "Massachusetts (or set the country first)")
            if len(answer) > STATE_TEXT_MAX:
                return "a state or province is at most %d characters" % STATE_TEXT_MAX
        elif builtin is None or answer not in builtin.options:
            return "pick one of the listed options"
    elif etype == "text":
        if len(answer) > TEXT_MAX:
            return "the answer is over %d characters" % TEXT_MAX
        if eid == "address_zip" and us is True and not _ZIP_RE.match(answer):
            return "a US ZIP code is 5 digits, or 5 digits, a hyphen and 4 more (12345-6789)"
    return None


def fact_value(entry: Any) -> str:
    """What a form gets for this answer: "" when it is not confirmed, not set, or
    does not fit its type; otherwise the answer (yes_no "Yes"/"No", number the
    digits, choice the option text, text the text). A yes/no or choice answer
    reads through `match_option`, so "yes " is "Yes". The only reader the run
    and the apply sheet use."""
    if not isinstance(entry, dict) or entry.get("confirmed") is not True:
        return ""
    if entry.get("status", "active") != "active":
        return ""
    answer = entry.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return ""
    eid = str(entry.get("id", "")).strip()
    etype = entry.get("type")
    if not _type_ok(eid, etype):
        return ""
    answer = answer.strip()
    if etype == "yes_no":
        answer = match_option(answer, YES_NO) or ""
    elif etype == "choice" and eid == "address_state":
        answer = match_option(answer, STATE_NAMES) or answer   # a province abroad is text
    elif etype == "choice":
        answer = match_option(answer, BUILTINS[eid].options) or ""
    if not answer or _answer_problem(eid, etype, answer, None) is not None:
        return ""
    return answer


# --- the seed -----------------------------------------------------------------------------

def _entry(eid: str, question: str, etype: str, answer: str, note: str = "",
           confirmed: bool = False) -> Dict[str, Any]:
    return {"id": eid, "question": question, "type": etype, "answer": answer, "note": note,
            "confirmed": confirmed, "status": "active"}


def _seed_answer(eid: str) -> str:
    value = apply_config.DEFAULTS.get(eid, "")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def seed_defaults() -> List[Dict[str, Any]]:
    """Every built-in in `BUILTINS` order with its seed value from
    `apply_config.DEFAULTS`, unconfirmed."""
    return [_entry(eid, b.question, b.type, _seed_answer(eid)) for eid, b in BUILTINS.items()]


def migrate_from_apply_config(answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply any overrides from a pre-existing apply_config.json onto matching
    built-ins, read the way a version 1 answer migrates. The confirmed flag is
    left alone. Idempotent: with no file, the merged config equals the defaults."""
    cfg = apply_config.load_apply_config()
    rows = [{"id": k, "question": BUILTINS[k].question, "answer": _v1_text(v)}
            for k, v in cfg.items() if k in BUILTINS]
    read = {e["id"]: e for e in migrate_v1(rows)[0]}
    for e in answers:
        got = read.get(e.get("id"))
        if got is not None:
            e["answer"], e["note"] = got["answer"], got["note"]
    return answers


def with_missing_builtins(answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Append each built-in the list lacks, not set and unconfirmed (never its
    seed value), so the editor always shows the full standard set."""
    have = {str(e.get("id", "")).strip() for e in answers if isinstance(e, dict)}
    for eid, b in BUILTINS.items():
        if eid not in have:
            answers.append(_entry(eid, b.question, b.type, ""))
    return answers


# --- migration from version 1 -------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_LEAD_PUNCT = re.compile(r"^[\s.,;:!?\-\u2013\u2014]+")
_BARE_YES_NO = re.compile(r"(yes|no|true|false|1|0)[.!]?")
_LEAD_NUMBER = re.compile(r"\d{1,2}(\.5)?")
# after a leading number: more digits, a decimal, a range, or a slash fraction
# ("3 1/2 years") makes it unreadable
_NUMBER_TAIL_BAD = re.compile(
    r"^(?:[.,]?\d|\s*(?:-|\u2013|\u2014|to\b|or\b)\s*\d|\s+\d+\s*/\s*\d+)", re.I)
# a years_experience answer with no leading digit that still means zero
_ZERO_EXPERIENCE_RE = re.compile(
    r"^(?:less than (?:1|one)|under 1|<\s?1|none|no experience)\b", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: Any) -> str:
    """Lowercase, apostrophes and every other non-alphanumeric run to one space."""
    s = str(text or "").lower().replace("\u2019", "'")
    return " ".join(_NON_ALNUM.sub(" ", s).split())


# Nested by built-in id, so an alias like "No" means one thing for
# veteran_status and another for disability_status, and a US state code
# ("GA") never leaks into matching a country.
_ALIASES = {eid: {_norm(k): v for k, v in aliases.items()}
            for eid, aliases in CHOICE_ALIASES.items()}


def _v1_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _match_option(eid: str, text: str, options: Tuple[str, ...]) -> Optional[str]:
    n = _norm(text)
    for option in options:
        if _norm(option) == n:
            return option
    alias = _ALIASES.get(eid, {}).get(n)
    return alias if alias in options else None


def _read_v1(eid: str, etype: str, raw: str, us: bool) -> Tuple[str, str, bool]:
    """(answer, note, readable) for one version 1 answer, already stripped."""
    if not raw:
        return "", "", True
    if etype == "yes_no":
        word = yes_no(raw)
        m = _WORD_RE.match(raw)
        if not word or not m:
            return "", raw, False
        return word, _LEAD_PUNCT.sub("", raw[m.end():]).strip(), True
    if etype == "number":
        m = _LEAD_NUMBER.match(raw)
        if m:
            rest = raw[m.end():]
            if not _NUMBER_TAIL_BAD.match(rest) and float(m.group(0)) <= NUMBER_MAX:
                return m.group(0), rest.strip(), True
        elif eid == "years_experience" and _ZERO_EXPERIENCE_RE.match(raw):
            return "0", raw, True
        return "", raw, False
    if etype == "choice":
        if eid == "address_state" and not us:
            return (raw, "", True) if len(raw) <= STATE_TEXT_MAX else ("", raw, False)
        hit = _match_option(eid, raw, BUILTINS[eid].options if eid in BUILTINS else ())
        return (hit, "", True) if hit else ("", raw, False)
    if eid == "address_zip" and us and not _ZIP_RE.match(raw):
        return "", raw, False
    if len(raw) > TEXT_MAX:
        return "", raw, False
    return raw, "", True


def _is_seed(eid: str, raw: str) -> bool:
    """The v1 answer is still what the v1 seed wrote for this id (the user never
    changed it). A yes/no seed also counts in its bare-word forms ("true", "Yes")."""
    if eid not in apply_config.DEFAULTS:
        return False
    seed = apply_config.DEFAULTS[eid]
    if isinstance(seed, bool):
        return (bool(_BARE_YES_NO.fullmatch(raw.lower()))
                and yes_no(raw) == ("Yes" if seed else "No"))
    return raw.casefold() == str(seed).strip().casefold()


def _reads_same(raw: str, answer: str, etype: str) -> bool:
    if etype == "yes_no" and _BARE_YES_NO.fullmatch(raw.lower()):
        return yes_no(raw) == answer
    return raw.casefold() == answer.casefold()


# The alias tables whose every entry is a spelling of its option: a state code,
# a way to write the United States.
_SPELLING_ALIAS_IDS = ("address_state", "address_country")


def _clean_read(eid: str, etype: str, raw: str, answer: str, note: str) -> bool:
    """The migration read `raw` with no guess about its meaning: the whole text
    is the value (case, spacing and end punctuation aside for a choice), or a
    spelling alias (a state code, "USA", a decline form). A worded yes/no, a
    number with words after it and any other alias are readings the user
    confirms."""
    if not answer or note:
        return False
    if _reads_same(raw, answer, etype):
        return True
    if etype != "choice":
        return False
    if _norm(raw) == _norm(answer):
        return True
    alias = _ALIASES.get(eid, {}).get(_norm(raw))
    return alias == answer and (eid in _SPELLING_ALIAS_IDS or answer == DECLINE)


def _describe(answer: str, note: str) -> str:
    text = answer or "Not set"
    return f"{text}, note '{note}'" if note else text


def migrate_v1(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Version 1 entries -> (version 2 entries, review items).

    A yes/no answer is read by its first word, the rest kept as the note; a number
    by its leading `\\d{1,2}(\\.5)?`, the rest kept as the note; a choice by its
    options (case aside), then `CHOICE_ALIASES`; custom entries become text. What
    cannot be read is left not set with the words in the note. An entry is
    confirmed when the user changed it from its seed and its text is the value
    itself or a spelling of it (`_clean_read`); a worded or aliased reading stays
    unconfirmed. Each converted or unreadable answer gets a review item {id,
    question, before, after}.
    """
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    country = next((_v1_text(e.get("answer")).strip() for e in rows
                    if str(e.get("id", "")).strip() == "address_country"), "")
    country = _read_v1("address_country", "choice", country, True)[0]
    us = country in ("", US_COUNTRY)
    out: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    for old in rows:
        eid = str(old.get("id", "")).strip()
        builtin = BUILTINS.get(eid)
        etype = builtin.type if builtin is not None else "text"
        question = builtin.question if builtin is not None else old.get("question", "")
        raw = _v1_text(old.get("answer")).strip()
        answer, note, readable = _read_v1(eid, etype, raw, us)
        changed = builtin is None or not _is_seed(eid, raw)
        confirmed = (readable and changed and old.get("status") != "needs-review"
                     and _clean_read(eid, etype, raw, answer, note))
        new = {"id": old.get("id", ""), "question": question, "type": etype, "answer": answer,
               "note": note, "confirmed": confirmed, "status": "active"}
        for key, value in old.items():
            new.setdefault(key, value)
        out.append(new)
        if raw and (not readable or note or not _reads_same(raw, answer, etype)):
            review.append({"id": new["id"], "question": question, "before": raw,
                           "after": _describe(answer, note)})
    return out, review


# --- load and save ------------------------------------------------------------------------

_REVIEW_KEYS = {"id", "question", "before", "after"}


def _read_file(path: Path) -> Dict[str, Any]:
    """The parsed file, shape-checked; AnswerStoreError when it cannot be used."""
    try:
        text = path.read_bytes().decode("utf-8-sig")   # a BOM-prefixed file still reads
    except OSError as exc:
        raise AnswerStoreError(path, f"it could not be read ({exc.strerror or exc})") from exc
    except UnicodeDecodeError as exc:
        raise AnswerStoreError(path, "it is not valid UTF-8 text") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnswerStoreError(
            path, f"it is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise AnswerStoreError(path, f"it is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise AnswerStoreError(path, "its top level is not a JSON object")
    version = data.get("version", 1)
    if isinstance(version, bool) or version not in (1, VERSION):
        raise AnswerStoreError(path, f"its version ({version!r}) is not one this app reads")
    answers = data.get("answers")
    if not isinstance(answers, list):
        raise AnswerStoreError(path, "it has no answers list")
    for i, e in enumerate(answers):
        if not isinstance(e, dict):
            raise AnswerStoreError(path, f"answer {i + 1} is not a record")
    review = data.get("review", [])
    if not isinstance(review, list):
        raise AnswerStoreError(path, "its review list is not a list")
    for i, r in enumerate(review):
        if (not isinstance(r, dict) or set(r) != _REVIEW_KEYS
                or not all(isinstance(v, str) for v in r.values())):
            raise AnswerStoreError(path, f"review item {i + 1} is not a valid record")
    return data


def load_store(path: Union[Path, None] = None) -> Dict[str, Any]:
    """{"answers", "review", "disk_version"}: a version 2 file as saved; a version 1
    file migrated in memory (its review from the migration); no file, the seeded
    defaults with `disk_version` None. Raises AnswerStoreError on a damaged file."""
    path = Path(path) if path is not None else STORE_PATH
    if not path.exists():
        return {"answers": migrate_from_apply_config(seed_defaults()), "review": [],
                "disk_version": None}
    data = _read_file(path)
    if data.get("version", 1) == 1:
        answers, review = migrate_v1(data["answers"])
        return {"answers": answers, "review": review, "disk_version": 1}
    return {"answers": data["answers"], "review": data.get("review", []),
            "disk_version": VERSION}


def load(path: Union[Path, None] = None) -> List[Dict[str, Any]]:
    """The stored answers exactly as saved (a v1 file migrated in memory), or the
    seeded defaults when there is no file. Raises AnswerStoreError on a damaged
    file. The editor calls `load_with_defaults` to get the full standard set."""
    return load_store(path)["answers"]


def load_with_defaults(path: Union[Path, None] = None) -> List[Dict[str, Any]]:
    """`load()` plus each built-in the store lacks, not set and unconfirmed."""
    return with_missing_builtins(load(path))


def _us_address(answers: List[Any]) -> bool:
    """The address follows the US rules: the country is the United States or not set."""
    for e in answers:
        if isinstance(e, dict) and str(e.get("id", "")).strip() == "address_country":
            return str(e.get("answer", "") or "").strip() in ("", US_COUNTRY)
    return True


def validate(answers: List[Dict[str, Any]]) -> List[str]:
    """Problems that block a save ([] = OK): the record shape, each answer's shape
    for its type, the address rules, a built-in's question and type, a custom
    question that is a built-in's own question or an earlier custom's
    (`find_collision`), duplicate ids."""
    errors: List[str] = []
    if not isinstance(answers, list):
        return ["the answer store must be a list of entries"]
    us = _us_address(answers)
    seen: set = set()
    for i, e in enumerate(answers):
        if not isinstance(e, dict):
            errors.append("entry %d is not a record" % (i + 1))
            continue
        eid = str(e.get("id", "")).strip()
        label = eid or ("entry %d" % (i + 1))
        question = str(e.get("question", "") or "").strip()
        etype = e.get("type")
        builtin = BUILTINS.get(eid)
        if not eid:
            errors.append("%s: id is required" % label)
        if not question:
            errors.append("answer '%s': question is required" % label)
        if e.get("status") not in STATUSES:
            errors.append("answer '%s': status must be active" % label)
        if not isinstance(e.get("confirmed"), bool):
            errors.append("answer '%s': confirmed must be true or false" % label)
        if builtin is not None:
            if question and question != builtin.question:
                errors.append("answer '%s': a built-in question keeps its own text (%r)"
                              % (label, builtin.question))
            if etype != builtin.type:
                errors.append("answer '%s': its type must be %s" % (label, builtin.type))
        elif etype not in CUSTOM_TYPES:
            errors.append("answer '%s': type must be one of %s"
                          % (label, ", ".join(CUSTOM_TYPES)))
        elif question:
            hit = find_collision(question, answers[:i], own_id=eid)
            if hit in _BUILTIN_QUESTIONS:
                errors.append("answer '%s': the built-in answer '%s' already answers this "
                              "question, and the run fills it from that answer" % (label, hit))
            elif hit:
                errors.append("answer '%s': the custom answer '%s' already has this "
                              "question; keep one of the two" % (label, hit))
        answer, note = e.get("answer", ""), e.get("note", "")
        if not isinstance(answer, str):
            errors.append("answer '%s': the answer must be text" % label)
        elif answer and _type_ok(eid, etype):
            problem = _answer_problem(eid, etype, answer, us)
            if problem:
                errors.append("answer '%s': %s" % (label, problem))
        if note is not None and not isinstance(note, str):
            errors.append("answer '%s': the note must be text" % label)
        elif etype in ("yes_no", "number") and len(note or "") > NOTE_MAX:
            errors.append("answer '%s': the note is %d of %d characters"
                          % (label, len(note), NOTE_MAX))
        if eid:
            if eid in seen:
                errors.append("duplicate answer id '%s'" % eid)
            seen.add(eid)
    return errors


def _plural(n: int, one: str, many: str) -> str:
    return one % n if n == 1 else many % n


def warnings(answers: List[Dict[str, Any]]) -> List[str]:
    """Warnings that never block a save: the authorization and
    sponsorship answers that disagree, built-ins not set, and a count of set
    built-ins not confirmed yet."""
    rows = {str(e.get("id", "")).strip(): e for e in answers if isinstance(e, dict)}

    def answer(eid: str) -> str:
        return str((rows.get(eid) or {}).get("answer", "") or "").strip()

    out: List[str] = []
    if answer("work_authorized") == "No" and answer("requires_sponsorship") == "No":
        out.append("'%s' and '%s' are both No; these two disagree: someone not authorized "
                   "to work usually needs sponsorship"
                   % (BUILTINS["work_authorized"].question,
                      BUILTINS["requires_sponsorship"].question))
    unset = [b.question for eid, b in BUILTINS.items() if not answer(eid)]
    if unset:
        out.append("Not set yet, so forms get nothing for these: " + "; ".join(unset))
    waiting = sum(1 for eid in BUILTINS
                  if answer(eid) and rows[eid].get("confirmed") is not True)
    if waiting:
        out.append(_plural(waiting, "%d answer is not confirmed yet",
                           "%d answers are not confirmed yet")
                   + "; the run uses only confirmed answers")
    return out


def save(answers: List[Dict[str, Any]], path: Union[Path, None] = None,
         review: Optional[List[Dict[str, Any]]] = None) -> None:
    """Validate, then atomically write {"version": 2, "answers", "review"}, backing
    up the current file to `<name>.bak` first. `review=None` keeps the review list
    a version 2 file has now ([] otherwise). Raises ValueError when the store is
    invalid. When the file on disk is damaged, raises AnswerStoreError and writes
    nothing."""
    errs = validate(answers)
    if errs:
        raise ValueError("; ".join(errs))
    path = Path(path) if path is not None else STORE_PATH
    if path.exists():
        disk = load_store(path)                  # raises on a damaged file
        if review is None and disk["disk_version"] == VERSION:
            review = disk["review"]
    payload = json.dumps({"version": VERSION, "answers": answers,
                          "review": list(review or [])}, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".answers_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        if path.exists():
            shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- editor helpers -----------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "answer").lower()).strip("_")
    return s or "answer"


def _unique_id(base: str, taken: set) -> str:
    cand, n = base, 2
    while cand in taken:
        cand = "%s_%d" % (base, n)
        n += 1
    return cand


def new_id(question: str, taken: set) -> str:
    """A unique slug id for a freshly added answer (used by the dashboard editor)."""
    return _unique_id(_slug(question), taken)


def find_collision(question: str, entries: List[Dict[str, Any]],
                   own_id: Optional[str] = None) -> Optional[str]:
    """The question text of the answer a (new or custom) question duplicates, or
    None: a built-in whose own question it is word for word, or a custom
    question with the same text (case, spacing and punctuation aside). `own_id`
    is the question's own entry, left out. A question the run would answer
    from a built-in in other words is the Add answer dialog's check
    (`apply_facts.question_fit`, under local/qt)."""
    norm = _norm(question)
    if not norm:
        return None
    for eid, b in BUILTINS.items():
        if eid != own_id and norm == _norm(b.question):
            return b.question
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id", "")).strip()
        if eid in BUILTINS or (own_id is not None and eid == own_id):
            continue
        if _norm(e.get("question")) == norm:
            return str(e.get("question", ""))
    return None


def restore_bytes(data: bytes, path: Union[Path, None] = None) -> None:
    """Overwrite the store with raw bytes (the editor's "revert to opening state"),
    backing the current file up to `<name>.bak` first when that current file is
    itself readable. A damaged current file is left out of `.bak`, so a good
    backup already there survives the revert."""
    path = Path(path) if path is not None else STORE_PATH
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".answers_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.exists():
            try:
                _read_file(path)
            except AnswerStoreError:
                pass
            else:
                shutil.copy2(str(path), str(path.with_name(path.name + ".bak")))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
