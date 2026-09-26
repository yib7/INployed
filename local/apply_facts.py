"""The fact catalog: what the candidate can truthfully put on a form.

`build(folder)` reads the job folder's `apply.md` (through
`apply_playwright.parse_apply_md` for the Candidate and signature blocks, plus
a small section reader here for Education, the first Work experience entry and
the Cover letter), takes every answer and the mailing address from the answer
store through `apply_answers.fact_value` (the sheet's Standard answers and
Address blocks only show what the store holds), and finds
the resume and cover letter PDFs in the folder. Every `Fact` carries
a `key`, its `value`, a `description` written the way a form label reads (the
judge matches labels against descriptions) and a `kind`.

`FactCatalog.to_criteria()` hands the mapping judge descriptions of facts
that have a value. `value(key)` supplies the value for filling and option
selection. `sheet_excerpt()` provides source prose for grounding drafts;
`verification_excerpt()` provides the catalog evidence for checking filled
values, with artifact paths reduced to filenames.

`quick_map()` is the deterministic first pass for unmistakable fields (a label
or id that says `first_name`, `email`, `resume`, ...). The judge still asks
its mapping question for every field; the planner prefers the deterministic
key and logs a disagreement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Iterable

from apply_form import FIELD_TYPES
from apply_playwright import parse_apply_md, split_name

KINDS = ("text", "bool", "choice_text", "file", "date")

# Descriptions read like the form labels they should match (a bool's shape is
# its `kind`; the description does not spell out yes / no). Order is the order
# the judge sees the options in; the common identity facts come first.
DESCRIPTIONS: dict[str, str] = {
    "full_name": "The candidate's full name, first and last together",
    "first_name": "The candidate's first (given) name",
    "last_name": "The candidate's last (family) name, the surname",
    "email": "The candidate's email address",
    "phone": "The candidate's phone number",
    "location": "The candidate's current city and state, as written on the resume",
    "address_street": "Street address, line 1 of the mailing address",
    "address_city": "City of the mailing address",
    "address_state": "State or province of the mailing address",
    "address_zip": "ZIP or postal code of the mailing address",
    "address_country": "Country of the mailing address",
    "linkedin_url": "The candidate's LinkedIn profile URL",
    # the sheet's "GitHub / Portfolio" row: a form's Portfolio URL takes it
    # (the live judge left "Portfolio URL" blank at 0.54 to 0.65 while this
    # said "GitHub profile URL" only, 2026-09-25)
    "github_url": "The candidate's GitHub or portfolio URL",
    "website_url": "The candidate's personal website or portfolio URL",
    "work_authorized": "Whether the candidate is legally authorized to work in the "
                       "United States",
    "requires_sponsorship": "Whether the candidate will now or in the future require "
                            "visa sponsorship",
    "years_experience": "Total years of professional work experience across all jobs "
                        "(not years with one skill, tool or language)",
    "willing_to_relocate": "Whether the candidate is willing to relocate to the job's location",
    "onsite_ok": "Whether the candidate is willing to work on-site in the employer's office, "
                 "in person",
    # derived from the store's yes / no answers (`DERIVED_YES_NO`)
    "authorized_without_sponsorship": "Whether the candidate can take a job without employer "
                                      "sponsorship, now and in the future, which is "
                                      "unrestricted work authorization",
    "remote_only": "Whether the candidate is looking for fully remote work only",
    "gender": "The candidate's gender, for EEO self-identification",
    "race_ethnicity": "The candidate's race or ethnicity, for EEO self-identification",
    "veteran_status": "The candidate's veteran status, for EEO self-identification",
    "disability_status": "The candidate's disability status, for EEO self-identification",
    "how_did_you_hear": "How the candidate heard about the job or the company",
    "education_school": "The school, college or university the candidate attended",
    "education_degree": "The degree the candidate earned (for example B.S. or M.S.)",
    "education_field": "The candidate's field of study or major",
    "education_grad_year": "The year the candidate graduated or expects to graduate",
    "current_company": "The candidate's current or most recent employer",
    "current_title": "The candidate's current or most recent job title",
    "resume_file": "The tailored resume PDF on disk, for a file upload",
    "cover_letter_file": "The cover letter PDF on disk, for a file upload",
    "cover_letter_text": "The cover letter as plain text, for a paste box",
    "signature_name": "The candidate's typed name for an electronic signature",
    "today": "Today's date in ISO form, for a signature date or application date",
}

# The store's yes/no answers (`apply_answers.BOOL_IDS`; a test pins the two
# lists equal). Their facts are `bool`.
_BOOL_BANK_IDS = frozenset(("work_authorized", "requires_sponsorship", "willing_to_relocate",
                            "onsite_ok"))
# Yes / no facts the run works out from the store's confirmed answers
# (cycle 18, SP6c), for the questions whose polarity or scope the stored ones
# do not answer ("authorized to work without sponsorship", "remote only").
# Never stored or edited; unset while an input is unset or unconfirmed.
DERIVED_YES_NO: tuple[str, ...] = ("authorized_without_sponsorship", "remote_only")
# every yes / no fact, in the order the judge reads them (`DESCRIPTIONS`), and
# the store's own among them
YES_NO_KEYS: tuple[str, ...] = tuple(k for k in DESCRIPTIONS
                                     if k in _BOOL_BANK_IDS or k in DERIVED_YES_NO)
STORED_YES_NO_KEYS: tuple[str, ...] = tuple(k for k in YES_NO_KEYS if k in _BOOL_BANK_IDS)

_KIND_BY_KEY: dict[str, str] = {
    **dict.fromkeys(YES_NO_KEYS, "bool"),
    "gender": "choice_text", "race_ethnicity": "choice_text", "veteran_status": "choice_text",
    "disability_status": "choice_text", "how_did_you_hear": "choice_text",
    "address_state": "choice_text", "address_country": "choice_text",
    "resume_file": "file", "cover_letter_file": "file", "today": "date",
}

# Answer-bank ids that ARE a named key (the DEFAULTS ids). Anything else in the
# bank becomes `answer_<id>`.
_NAMED_BANK_IDS = frozenset((
    "work_authorized", "requires_sponsorship", "years_experience", "willing_to_relocate",
    "onsite_ok", "gender", "race_ethnicity", "veteran_status", "disability_status",
    "how_did_you_hear",
    "address_street", "address_city", "address_state", "address_zip", "address_country",
))

# parse_apply_md lowercases the sheet's labels; these are the ones it emits.
_CANDIDATE_LABELS = {"full_name": "name", "email": "email", "phone": "phone",
                     "location": "location", "linkedin_url": "linkedin",
                     "github_url": "github / portfolio", "website_url": "website"}
_BASICS_KEYS = {"full_name": "name", "email": "email", "phone": "phone",
                "location": "location", "linkedin_url": "linkedin",
                "github_url": "github", "website_url": "website"}

_H2_RE = re.compile(r"(?m)^##\s+(?P<name>[^\n]+?)\s*$")
_ADDRESS_H3_RE = re.compile(r"(?m)^###\s+Address\s*$")
_EM_DASH = chr(0x2014)
_DOT = chr(0xB7)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_ENTRY_HEADER_RE = re.compile(r"^\*\*(?P<org>.+?)\*\*(?:\s+" + _EM_DASH + r"\s+(?P<rest>.*))?$")
_UNESCAPE_RE = re.compile(r"(?m)^(\s*)\\(#{1,6}\s|-\s+\*\*)")


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    description: str
    kind: str = "text"


class FactCatalog:
    """The facts for one job folder, keyed by fact key."""

    def __init__(self, facts: Iterable[Fact] = (), sheet_text: str = "",
                answers: list[dict] | None = None):
        self.facts: dict[str, Fact] = {f.key: f for f in facts}
        self._sheet_text = sheet_text or ""
        self._bank = answers or []

    def has(self, key: str) -> bool:
        f = self.facts.get(key)
        return bool(f and f.value)

    def value(self, key: str) -> str:
        f = self.facts.get(key)
        return f.value if f else ""

    def custom_type(self, key: str) -> str:
        """The store type of a custom answer's fact (`answer_<id>`): a gated
        type (`GATED_CUSTOM_TYPES`) when any entry with that id has one, else
        the entry's type; "" for any other fact."""
        if not key.startswith("answer_") or key not in self.facts:
            return ""
        eid = key[len("answer_"):]
        types = [str(e.get("type", "") or "") for e in self._bank
                 if isinstance(e, dict) and str(e.get("id", "")).strip() == eid]
        return next((t for t in types if t in GATED_CUSTOM_TYPES), types[0] if types else "")

    def answers_field(self, key: str, label: str, help_text: str = "", *,
                      value: str | None = None, partial: bool = False) -> bool:
        """Does `key`'s value (this catalog's, or `value`) answer a field with
        this label and help? A custom yes / no or number answer answers only
        its own saved question, word for word (`same_question`); every other
        fact goes through `answers_question`. A cut label (`partial`) answers
        neither: the words it lost are unread."""
        if self.custom_type(key) in GATED_CUSTOM_TYPES:
            return not partial and same_question(label, help_text, self.facts[key].description)
        return answers_question(key, self.value(key) if value is None else value, label,
                                help_text, partial)

    def to_criteria(self) -> dict[str, str]:
        """key -> description for every fact that has a value. Never a value."""
        return {k: f.description for k, f in self.facts.items() if f.value}

    def sheet_excerpt(self, max_chars: int = 6000) -> str:
        """The candidate, answers, education and achievement source sections
        for grounding drafts. The Standard answers section and the Address
        block are rendered fresh from the answer store (the same renderer
        `apply_data` uses for the sheet), never read from the sheet's own
        text: a sheet a refresh left stale never hands a drafting call an
        answer the store does not currently confirm. Generated cover-letter
        prose is excluded. Longer than `max_chars`, it is cut at the last
        line end at or before the cap (a first line longer than the cap is
        cut at the cap)."""
        from resume_tailor import apply_data  # lazy: config.py loads .env at import
        sections = _h2_sections(self._sheet_text)
        parts = []
        for name in ("candidate", "standard answers", "education",
                    "work experience", "projects", "leadership"):
            if name not in sections:
                continue
            if name == "candidate":
                address = apply_data._address_lines(self._bank).strip()
                candidate = _strip_nested_address(sections[name])
                parts.append(candidate + (f"\n\n{address}" if address else ""))
            elif name == "standard answers":
                parts.append(apply_data._standard_answer_lines(self._bank).strip())
            else:
                parts.append(sections[name])
        text = "\n\n".join(parts).strip()
        if len(text) <= max_chars:
            return text
        cut = text.rfind("\n", 0, max_chars + 1)
        return text[:cut] if cut > 0 else text[:max_chars]

    def verification_excerpt(self, fact_keys: Iterable[str] | None = None) -> str:
        """Evidence for filled values from the current catalog, including bank
        fallbacks and the selected artifacts. File values use only the basename
        on either Windows or POSIX. Callers can select the relevant keys; selected
        evidence is kept in full so a length cap cannot silently remove a fact."""
        keys = self.facts if fact_keys is None else dict.fromkeys(fact_keys)
        rows = []
        for key in keys:
            fact = self.facts.get(key)
            if fact is None or not fact.value:
                continue
            value = PureWindowsPath(fact.value).name if fact.kind == "file" else fact.value
            rows.append(f"- {fact.description} ({key}): {value}")
        return "\n".join(rows)


def build(folder: Path, *, answers: list[dict] | None = None,
          master_basics: dict | None = None,
          today: date | None = None) -> FactCatalog:
    """The catalog for `folder`: its apply.md, the answer bank (`answers`, else
    the store via `resume_tailor.apply_answers.load()`), the master's basics as
    a fallback for the identity facts, and the folder's PDFs. Every answer and
    the address come from the bank through `apply_answers.fact_value` only, so
    an answer the user has not confirmed gives no fact."""
    from resume_tailor import apply_answers  # lazy: config.py loads .env at import
    folder = Path(folder)
    apply_md = folder / "apply.md"
    text = apply_md.read_text(encoding="utf-8") if apply_md.exists() else ""
    parsed = parse_apply_md(text)
    basics = {k: str(v or "").strip() for k, v in (master_basics or {}).items()}
    if answers is None:
        answers = apply_answers.load()
    bank = [e for e in (answers or [])
            if isinstance(e, dict) and str(e.get("status", "active")) == "active"]
    values: dict[str, str] = {}

    # Identity: the sheet's Candidate block, the master basics as a fallback.
    candidate = parsed.get("candidate", {})
    for key, label in _CANDIDATE_LABELS.items():
        values[key] = candidate.get(label, "") or basics.get(_BASICS_KEYS[key], "")
    first, last = split_name(values["full_name"])
    values["first_name"], values["last_name"] = first, last

    # The answers and the address: the store's, through `fact_value` ("" when
    # an answer is not set, not confirmed or does not fit its type). A sheet
    # written before a change in the store never outranks it (cycle 18). A
    # duplicate id takes its value from the FIRST entry that carries it (a
    # well-formed store never has one; `validate` rejects it) and ignores any
    # entry after it, confirmed or not.
    answer_facts: list[Fact] = []
    seen_named: set[str] = set()
    for entry in bank:
        eid = str(entry.get("id", "")).strip()
        if not eid:
            continue
        question = str(entry.get("question", "") or "").strip()
        value = apply_answers.fact_value(entry)
        if eid in _NAMED_BANK_IDS:
            if eid in seen_named:
                continue
            seen_named.add(eid)
            values[eid] = value
        else:
            answer_facts.append(Fact(key=f"answer_{eid}", value=value,
                                     description=question or eid.replace("_", " "),
                                     kind="text"))

    values.update(_derived(values))

    sections = _h2_sections(text)
    values.update(_education(sections.get("education", "")))
    values.update(_current_job(sections.get("work experience", "")))
    values["cover_letter_text"] = _UNESCAPE_RE.sub(
        r"\1\2", _body(sections.get("cover letter", ""))).strip()
    values["resume_file"], values["cover_letter_file"] = _pdfs(folder)
    values["signature_name"] = parsed.get("signature_name", "") or values["full_name"]
    values["today"] = (today or date.today()).isoformat()

    facts = [Fact(key=k, value=values.get(k, ""), description=desc,
                  kind=_KIND_BY_KEY.get(k, "text"))
             for k, desc in DESCRIPTIONS.items()]
    return FactCatalog(facts + answer_facts, sheet_text=text, answers=bank)


def _yes(value: str) -> bool | None:
    """A stored yes / no answer as True / False; None when it is unset."""
    v = (value or "").strip().lower()
    return True if v == "yes" else False if v == "no" else None


def _derived(values: dict[str, str]) -> dict[str, str]:
    """`DERIVED_YES_NO` from the confirmed answers in `values` ("" when an
    input is unset or unconfirmed). `authorized_without_sponsorship` is Yes
    for authorized and no sponsorship, else No; `remote_only` is the inverse
    of `onsite_ok`."""
    auth, sponsor = _yes(values.get("work_authorized", "")), \
        _yes(values.get("requires_sponsorship", ""))
    onsite = _yes(values.get("onsite_ok", ""))
    return {
        "authorized_without_sponsorship": (
            "" if auth is None or sponsor is None
            else "Yes" if auth and not sponsor else "No"),
        "remote_only": "" if onsite is None else "No" if onsite else "Yes",
    }


def _strip_nested_address(candidate_section: str) -> str:
    """The `## Candidate` section's text with its nested `### Address`
    sub-block (build_markdown puts it right after the candidate fields) cut
    off, so `sheet_excerpt` can splice in the store's own address rendering
    instead of the sheet's stale one."""
    m = _ADDRESS_H3_RE.search(candidate_section)
    return candidate_section[:m.start()].rstrip() if m else candidate_section


def _h2_sections(text: str) -> dict[str, str]:
    """Lowercased `## ` heading -> the body up to the next `## ` heading. The
    first occurrence of a heading wins (parse_apply_md's rule for a repeat)."""
    out: dict[str, str] = {}
    matches = list(_H2_RE.finditer(text or ""))
    for i, m in enumerate(matches):
        name = m.group("name").strip().lower()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.setdefault(name, text[m.start():end].strip())
    return out


def _body(block: str) -> str:
    """A section block without its heading line."""
    _, _, rest = block.partition("\n")
    return rest


def _education(section: str) -> dict[str, str]:
    """The first `- School <em dash> Degree, Field <dot> ... dates ...` line."""
    out = {"education_school": "", "education_degree": "", "education_field": "",
           "education_grad_year": ""}
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("- ") or s.startswith("- (none"):
            continue
        body = s[2:].strip()
        school, _, rest = body.partition(_EM_DASH)
        school = school.strip()
        if not school:
            continue
        out["education_school"] = school.split(_DOT)[0].strip()
        chunks = [c.strip() for c in rest.split(_DOT)] if rest else []
        if chunks and chunks[0]:
            degree, _, field_ = chunks[0].partition(",")
            out["education_degree"] = degree.strip()
            out["education_field"] = field_.strip()
        out["education_grad_year"] = _grad_year(chunks[1:])
        break
    return out


_EXPECTED_RE = re.compile(r"expected\s+(?:[A-Za-z]+\.?\s+)?((?:19|20)\d{2})\b", re.I)
_UNDER_WAY_RE = re.compile(r"\b(present|current|now)\b[\s).]*$", re.I)


def _grad_year(chunks: list[str]) -> str:
    """An education line's graduation year (cycle 18, FM-8): the expected
    year when the line names one ("Expected May 2026"); none for a degree
    under way ("2022 - Present"); else the last year on the line."""
    text = " ".join(chunks)
    m = _EXPECTED_RE.search(text)
    if m:
        return m.group(1)
    if any(_UNDER_WAY_RE.search(c) for c in chunks):
        return ""
    years = _YEAR_RE.findall(text)
    return years[-1] if years else ""


def _current_job(section: str) -> dict[str, str]:
    """The first `**Org** <em dash> Title <dot> Location <dot> Dates` header."""
    out = {"current_company": "", "current_title": ""}
    for line in section.splitlines():
        m = _ENTRY_HEADER_RE.match(line.strip())
        if not m:
            continue
        out["current_company"] = m.group("org").strip()
        rest = m.group("rest") or ""
        out["current_title"] = rest.split(_DOT)[0].strip()
        break
    return out


def _pdfs(folder: Path) -> tuple[str, str]:
    """(resume, cover letter): the newest PDF whose stem contains `cover` is the
    letter, the newest other PDF the resume; "" when absent."""
    try:
        pdfs = sorted((p for p in folder.glob("*.pdf") if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return "", ""
    cover = next((p for p in pdfs if "cover" in p.stem.lower()), None)
    resume = next((p for p in pdfs if "cover" not in p.stem.lower()), None)
    return (str(resume) if resume else ""), (str(cover) if cover else "")


# --- quick_map --------------------------------------------------------------------

_TEXTISH = frozenset(("text", "email", "tel", "url", "textarea", "other", ""))
_NOT_FILE = (frozenset(FIELD_TYPES) - {"file"}) | {""}
_FILE_ONLY = frozenset(("file",))
_NORM_RE = re.compile(r"[^a-z0-9]+")

# (phrase tokens, key, allowed types); checked in order, label before id.
_QUICK: tuple[tuple[tuple[str, ...], str, frozenset[str]], ...] = (
    (("first", "name"), "first_name", _TEXTISH),
    (("given", "name"), "first_name", _TEXTISH),
    (("last", "name"), "last_name", _TEXTISH),
    (("surname",), "last_name", _TEXTISH),
    (("family", "name"), "last_name", _TEXTISH),
    (("cover", "letter"), "cover_letter_file", _FILE_ONLY),
    (("resume",), "resume_file", _FILE_ONLY),
    (("cv",), "resume_file", _FILE_ONLY),
    (("email",), "email", _TEXTISH),
    (("phone",), "phone", _TEXTISH),
    (("mobile",), "phone", _TEXTISH),
    (("telephone",), "phone", _TEXTISH),
    (("linkedin",), "linkedin_url", _TEXTISH),
    (("github",), "github_url", _TEXTISH),
    (("website",), "website_url", _TEXTISH),
    (("portfolio",), "website_url", _TEXTISH),
)
# The address tokens are single common words ("State your desired salary",
# "Country of work authorization"), so they only hit a short label with no
# other content noun; the model maps the rest.
_ADDRESS_QUICK: tuple[tuple[tuple[str, ...], str, frozenset[str]], ...] = (
    (("city",), "address_city", _NOT_FILE),
    (("state",), "address_state", _NOT_FILE),
    (("province",), "address_state", _NOT_FILE),
    (("zip",), "address_zip", _NOT_FILE),
    (("postal",), "address_zip", _NOT_FILE),
    (("country",), "address_country", _NOT_FILE),
)
_ADDRESS_MAX_TOKENS = 4
_ADDRESS_STOP = frozenset((
    "salary", "authorization", "authorized", "work", "desired", "preferred", "citizenship",
    "company", "employer", "user", "username"))
_NAME_ALONE = (("name",), ("full", "name"), ("your", "name"))


# another person's field (cycle 18, FM-3): a referrer's, an emergency
# contact's, a manager's; and the how-did-you-hear question, whose listed
# sources ("LinkedIn, website") are no profile of the candidate's
_OTHER_PERSON = re.compile(r"\b(referr\w*|referral|reference|emergency|manager|supervisor|spouse"
                           r"|recruiter|parent|guardian|next of kin)\b")
_HEAR_ABOUT = re.compile(r"\b(how did you (hear|find)|hear about|referral source|source)\b")
_PROFILE_URLS = frozenset(("linkedin_url", "github_url", "website_url"))
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _person_text(text: str) -> str:
    """`text` for the FM-3 guards: camelCase split, lowercased, and every
    run of other characters a single space ("emergencyContactPhone" reads
    "emergency contact phone")."""
    return " ".join(_NORM_RE.sub(" ", _CAMEL.sub(" ", text or "").lower()).split())


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _NORM_RE.sub(" ", (text or "").lower()).split() if t)


def _contains(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def quick_map(label: str, id_or_name: str, type_: str) -> str | None:
    """`_quick_key`, and None for a label its fact does not answer
    (`asks_own_question`, cycle 18 SP6c): the judge maps that one, to a
    derived fact when one answers it."""
    key = _quick_key(label, id_or_name, type_)
    return key if key is None or asks_own_question(key, label) else None


def _quick_key(label: str, id_or_name: str, type_: str) -> str | None:
    """The fact key for an unmistakable field, else None. Both strings are
    lowercased, non-alphanumerics become spaces, and a phrase has to appear as
    whole tokens (`email` matches "Email address", never "emailing"). `name`
    alone (`name`, `full name`, `your name`) means the full name; `first name`
    and `company name` are their own cases. The identity phrases apply to
    text-like controls only, `resume` / `cv` / `cover letter` to file inputs
    only. An address token (`city`, `state`, `zip`, ...) hits only a text of at
    most `_ADDRESS_MAX_TOKENS` tokens with none of `_ADDRESS_STOP` in it.

    A field whose label or id names another person ("Referrer email",
    "emergencyContactPhone", `_OTHER_PERSON`) is None: the judge reads it.
    A how-did-you-hear question (`_HEAR_ABOUT`) never maps to a profile URL,
    whatever sources it lists (cycle 18, FM-3)."""
    type_ = (type_ or "").lower()
    texts = [_person_text(label), _person_text(id_or_name)]
    if any(_OTHER_PERSON.search(t) for t in texts):
        return None
    hear = any(_HEAR_ABOUT.search(t) for t in texts)
    for text in (label, id_or_name):
        tokens = _tokens(text)
        if not tokens:
            continue
        for phrase, key, types in _QUICK:
            if hear and key in _PROFILE_URLS:
                continue
            if type_ in types and _contains(tokens, phrase):
                return key
        if type_ in _TEXTISH and tokens in _NAME_ALONE:
            return "full_name"
        if len(tokens) <= _ADDRESS_MAX_TOKENS and not _ADDRESS_STOP & set(tokens):
            for phrase, key, types in _ADDRESS_QUICK:
                if type_ in types and _contains(tokens, phrase):
                    return key
    return None


# --- a fact's own question (cycle 18, SP6c) -------------------------------------------

# Code fills or settles a yes / no or number fact only when the field asks that
# fact's own question: every content word of the field's label and help
# belongs to the fact's own-question vocabulary (round 3). A saved "Yes" to
# "authorized to work?" is no answer to "authorized to work? (Without
# sponsorship)", and total years are no answer to "Python - years of
# experience". The text is read whole (`question_words`): examples and
# neutral tails are dropped, set phrases become one token (`_PHRASES`), the
# shared filler goes, and any word left outside the fact's vocabulary makes it
# another question: a country, a city, a skill, "commute", "assistance",
# "comfortable", "rather", "clearance", a parenthetical scope or an
# instruction such as "Answer No if ...".

@dataclass(frozen=True)
class _OwnQuestion:
    vocabulary: frozenset[str]               # every word its own question may use
    # the subject: each set must share a word with the question
    topic: tuple[frozenset[str], ...]
    digits: bool = False                     # a number is one of its words (years)
    # a narrower form of its question, and the value that answers every
    # narrower form too (no sponsorship now or in the future is No to H-1B
    # sponsorship and to sponsorship now; on-site work is Yes to hybrid work)
    narrower: frozenset[str] = frozenset()
    settles_narrower: str = ""


# the words no question turns on
_FILLER = frozenset((
    "are", "you", "do", "does", "will", "would", "be", "to", "the", "a", "an", "of", "in",
    "for", "your", "legally", "able", "i", "am", "can", "have", "has"))
# a place name set beside "location" names the job's location: "the job
# location (New York)" (read before the text is lowercased)
_PLACE = re.compile(r"\b([Ll]ocation)\s*\([A-Z][\w.'-]*(?:,?\s+[A-Z][\w.'-]*)*\)")
_ABBREV = re.compile(r"\b(?:[a-z]\.){2,}")              # u.s. -> us, e.g. -> eg
# an example is no scope: "(e.g., H-1B visa status)", "such as an H-1B"
_EXAMPLE = re.compile(r"\((?:eg|for example|for instance|such as)\b[^)]*\)"
                      r"|\b(?:eg|for example|for instance|such as)\b[^.?!;()]*")
# a neutral tail or lead: "(If not, please explain.)", "Please select one",
# "(Yes/No)", "Please enter", "Required", "*"; "in order to" reads "to"
_NEUTRAL = re.compile(r"\(?\bif (?:not|yes|so),? please (?:explain|specify)\b[.:]?\)?"
                      r"|\(?\b(?:please )?(?:select|choose) one\b[.:]?\)?"
                      r"|\(?\b(?:please )?enter a (?:whole )?number\b[.:]?\)?"
                      r"|\(?\byes(?: ?/ ?| or )no\b\)?|\bplease (?:enter|provide|indicate)\b"
                      r"|\(required\)|\bin order(?= to\b)")
_REQUIRED_TAIL = re.compile(r"(?m)(?<=[?.*:\n-])\s*required\s*$")
# set phrases, each one token, in this order
_PHRASES: tuple[tuple[str, re.Pattern], ...] = tuple((token, re.compile(p)) for token, p in (
    ("NOSPONSOR", r"\bwithout (?:the need (?:for|of) |needing |requiring |any )?"
                  r"(?:(?:employer|employment|visa|company|work|any) )*sponsorship\b"
                  r"|\b(?:(?:will|do|does|would) not|won't|don't|doesn't|wouldn't) "
                  r"(?:now or in the future )?(?:need|require) (?:any )?"
                  r"(?:(?:employer|employment|visa|work) )*sponsorship\b"),
    ("UNRESTRICTED", r"\bwithout (?:any )?restrictions?\b|\bunrestricted\b"),
    ("NOWFUTURE", r"\b(?:now|currently),? (?:or|and),? (?:will you )?(?:at any time )?"
                  r"in (?:the )?future\b|\bnow or (?:at any time|later)\b|\bat any time\b"),
    ("FUTURE", r"\bin (?:the )?future\b"),
    ("NOW", r"\bright now\b|\bat this time\b|\bat present\b|\bpresently\b|\bcurrently\b"
            r"|\bnow\b"),
    ("STARTDATE", r"\b(?:on|by|as of) (?:your|the) start date\b"),
    ("US", r"\b(?:in|within|inside) (?:the )?(?:united states(?: of america)?|usa|us|america)\b"
           r"|\b(?:the )?united states(?: of america)?\b|\busa\b|\bamerica\b"),
    ("ONSITE", r"\bon[- ]?site\b|\bin[- ]person\b|\bin (?:the |our |an )?office\b"),
    ("DAYSWEEK", r"\b(?:\d+|one|two|three|four|five|six|seven)"
                 r"(?:\s*(?:-|to|or)\s*(?:\d+|one|two|three|four|five|six|seven))?"
                 r" days? (?:a|per|each|every) week\b"),
    ("VISATYPE", r"\b(?:h-?1b?|h-?4|l-?1[ab]?|o-?1[ab]?|e-?3|f-?1|j-?1|tn|opt|cpt|ead)\b"),
))
_TOKEN = re.compile(r"[A-Z]+|[a-z0-9]+(?:'[a-z]+)?")
# a word of later: with one, "now" asks now or later
_LATER = frozenset(("FUTURE", "NOWFUTURE"))

_AUTHORIZED = frozenset(("authorized", "authorised", "authorization", "authorisation",
                         "eligible", "permitted", "allowed", "right", "legal", "lawfully"))
_WORK = frozenset(("work", "working", "employment", "employed"))
# "us" is a word of the vocabulary ("to work for us"); only US, the phrase,
# names the country
_USA = frozenset(("US", "us"))
_WORK_AUTHORIZED = _AUTHORIZED | _WORK | _USA | {"take", "up", "STARTDATE", "NOW"}
_YEARS = frozenset(("years", "year", "yrs", "yr"))

OWN_QUESTIONS: dict[str, _OwnQuestion] = {
    # authorized, or work in the US: (A or W) and (A or US)
    "work_authorized": _OwnQuestion(
        _WORK_AUTHORIZED, (_AUTHORIZED | _WORK, _AUTHORIZED | {"US"})),
    "requires_sponsorship": _OwnQuestion(
        frozenset(("require", "need", "sponsorship", "sponsor", "visa", "employment", "based",
                   "status", "company", "employer", "work", "working", "continue", "this",
                   "or", "US", "us", "NOWFUTURE", "FUTURE", "NOW", "VISATYPE",
                   # "require the Company to commence ("sponsor") an immigration
                   # case in order to employ you"
                   "commence", "immigration", "case", "petition", "employ")),
        (frozenset(("sponsorship", "sponsor")),),
        narrower=frozenset(("NOW", "VISATYPE")), settles_narrower="No"),
    "authorized_without_sponsorship": _OwnQuestion(
        _WORK_AUTHORIZED | {"NOSPONSOR", "UNRESTRICTED", "and", "NOWFUTURE"},
        (frozenset(("NOSPONSOR", "UNRESTRICTED")),)),
    "willing_to_relocate": _OwnQuestion(
        frozenset(("relocate", "relocating", "relocation", "move", "moving", "willing", "open",
                   "consider", "job's", "job", "location", "position", "role", "this", "NOW")),
        (frozenset(("relocate", "relocating", "relocation", "move", "moving")),)),
    "onsite_ok": _OwnQuestion(
        frozenset(("ONSITE", "office", "hybrid", "work", "working", "our", "at", "into", "come",
                   "report", "from", "DAYSWEEK", "schedule", "willing", "open", "NOW")),
        (frozenset(("ONSITE", "office", "hybrid")),),
        narrower=frozenset(("hybrid",)), settles_narrower="Yes"),
    "remote_only": _OwnQuestion(
        frozenset(("remote", "remotely", "only", "fully", "looking", "seeking", "role", "roles",
                   "position", "positions", "require", "open", "work", "willing", "NOW")),
        (frozenset(("remote", "remotely")), frozenset(("only", "require")))),
    "years_experience": _OwnQuestion(
        _YEARS | {"experience", "how", "many", "much", "number", "relevant", "professional",
                  "work", "working", "total", "overall", "industry", "related", "full", "time",
                  "fulltime", "paid", "practical", "prior", "previous", "job", "employment",
                  "career", "combined", "similar", "role", "position", "capacity", "this",
                  "field", "at", "least", "or", "more", "plus", "NOW"},
        (_YEARS, frozenset(("experience",))), digits=True),
}
OWN_QUESTION_KEYS = frozenset(OWN_QUESTIONS)
# the custom answer types code settles: only for their own saved question
GATED_CUSTOM_TYPES = ("yes_no", "number")


def question_words(label: str, help_text: str = "") -> tuple[str, ...]:
    """The content words of a field's label and help, in order: lowercased,
    with a place name beside "location", examples ("e.g. H-1B", "such as
    ...") and neutral tails ("If not, please explain", "Please select one",
    "Required", "*") dropped, each set phrase as one token (`_PHRASES`: now
    or in the future is NOWFUTURE, in the United States is US, without
    sponsorship is NOSPONSOR, without restriction is UNRESTRICTED, on-site
    or in the office is ONSITE, 3 days a week is DAYSWEEK, a visa type is
    VISATYPE, ...) and the filler (`_FILLER`) removed."""
    text = _PLACE.sub(r"\1", f"{label or ''}\n{help_text or ''}")
    text = text.lower().replace(chr(0x2019), "'")
    text = _ABBREV.sub(lambda m: m.group(0).replace(".", ""), text)
    text = _EXAMPLE.sub(" ", text)
    text = _NEUTRAL.sub(" ", text)
    text = _REQUIRED_TAIL.sub(" ", text).replace("*", " ")
    for token, pattern in _PHRASES:
        text = pattern.sub(f" {token} ", text)
    return tuple(w for w in _TOKEN.findall(text) if w not in _FILLER)


def question_fit(fact_key: str | None, label: str, help_text: str = "",
                 partial: bool = False) -> str:
    """How a field with this label and help relates to `fact_key`'s own
    question (`OWN_QUESTIONS`): "own", "narrower" (a narrower form of it:
    H-1B sponsorship, sponsorship now, hybrid work) or "other". A fact with
    no table is always "own".

    Every content word (`question_words`) must be one of the fact's
    vocabulary and the words must name its subject (`topic`); a word of
    now beside a word of later asks now or later. A cut label (`partial`)
    is "other": the words it lost are unread."""
    spec = OWN_QUESTIONS.get(fact_key or "")
    if spec is None:
        return "own"
    if partial:
        return "other"
    words = set(question_words(label, help_text))
    if "NOW" in words and words & _LATER:
        words.discard("NOW")
    if spec.digits:
        words = {w for w in words if not w.isdigit()}
    if not words <= spec.vocabulary or not all(words & t for t in spec.topic):
        return "other"
    return "narrower" if words & spec.narrower else "own"


def asks_own_question(fact_key: str | None, label: str, help_text: str = "",
                      partial: bool = False) -> bool:
    """Does a field with this label and help ask `fact_key`'s own question
    (`question_fit` is "own")? A fact with no table always does."""
    return question_fit(fact_key, label, help_text, partial) == "own"


def answers_question(fact_key: str | None, value: str, label: str,
                     help_text: str = "", partial: bool = False) -> bool:
    """Does `value` of `fact_key` answer the field's question? Its own
    question, for any value; a narrower form only for the value that answers
    every narrower form (`settles_narrower`: a stored No to sponsorship now
    or in the future is No to "Will you require H-1B sponsorship?", and a
    Yes there is no answer); another question, never."""
    fit = question_fit(fact_key, label, help_text, partial)
    if fit == "narrower":
        settles = OWN_QUESTIONS[fact_key].settles_narrower
        return bool(settles) and (value or "").strip().lower() == settles.lower()
    return fit == "own"


def same_question(label: str, help_text: str, saved: str) -> bool:
    """Does a field with this label and help ask `saved` word for word
    (`question_words` as sets, with at least one word)? A custom yes / no or
    number answer settles only its own saved question."""
    words = set(question_words(label, help_text))
    return bool(words) and words == set(question_words(saved))
