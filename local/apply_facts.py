"""The fact catalog: what the candidate can truthfully put on a form.

`build(folder)` reads the job folder's `apply.md` (through
`apply_playwright.parse_apply_md` for the Candidate / Address / Standard
answers / signature blocks, plus a small section reader here for Education,
the first Work experience entry and the Cover letter), merges the answer bank,
and finds the resume and cover letter PDFs in the folder. Every `Fact` carries
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
    "years_experience": "How many years of relevant work experience the candidate has",
    "willing_to_relocate": "Whether the candidate is willing to relocate",
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

_KIND_BY_KEY: dict[str, str] = {
    "work_authorized": "bool", "requires_sponsorship": "bool", "willing_to_relocate": "bool",
    "gender": "choice_text", "race_ethnicity": "choice_text", "veteran_status": "choice_text",
    "disability_status": "choice_text", "how_did_you_hear": "choice_text",
    "address_state": "choice_text", "address_country": "choice_text",
    "resume_file": "file", "cover_letter_file": "file", "today": "date",
}

# Answer-bank ids that ARE a named key (the DEFAULTS ids). Anything else in the
# bank becomes `answer_<id>`.
_NAMED_BANK_IDS = frozenset((
    "work_authorized", "requires_sponsorship", "years_experience", "willing_to_relocate",
    "gender", "race_ethnicity", "veteran_status", "disability_status", "how_did_you_hear",
    "address_street", "address_city", "address_state", "address_zip", "address_country",
))
_BOOL_BANK_IDS = frozenset(("work_authorized", "requires_sponsorship", "willing_to_relocate"))

# parse_apply_md lowercases the sheet's labels; these are the ones it emits.
_CANDIDATE_LABELS = {"full_name": "name", "email": "email", "phone": "phone",
                     "location": "location", "linkedin_url": "linkedin",
                     "github_url": "github / portfolio", "website_url": "website"}
_ADDRESS_LABELS = {"address_street": "street", "address_city": "city",
                   "address_state": "state / province", "address_zip": "zip / postal",
                   "address_country": "country"}
_BASICS_KEYS = {"full_name": "name", "email": "email", "phone": "phone",
                "location": "location", "linkedin_url": "linkedin",
                "github_url": "github", "website_url": "website"}

_H2_RE = re.compile(r"(?m)^##\s+(?P<name>[^\n]+?)\s*$")
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

    def __init__(self, facts: Iterable[Fact] = (), sheet_text: str = ""):
        self.facts: dict[str, Fact] = {f.key: f for f in facts}
        self._sheet_text = sheet_text or ""

    def has(self, key: str) -> bool:
        f = self.facts.get(key)
        return bool(f and f.value)

    def value(self, key: str) -> str:
        f = self.facts.get(key)
        return f.value if f else ""

    def to_criteria(self) -> dict[str, str]:
        """key -> description for every fact that has a value. Never a value."""
        return {k: f.description for k, f in self.facts.items() if f.value}

    def sheet_excerpt(self, max_chars: int = 6000) -> str:
        """The candidate, answers, education and achievement source sections
        for grounding drafts. Generated cover-letter prose is excluded. Longer than
        `max_chars`, it is cut at the last line end at or before the cap (a
        first line longer than the cap is cut at the cap)."""
        sections = _h2_sections(self._sheet_text)
        parts = [sections[name] for name in ("candidate", "standard answers", "education",
                                             "work experience", "projects", "leadership")
                 if name in sections]
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
    a fallback for the identity facts, and the folder's PDFs."""
    folder = Path(folder)
    apply_md = folder / "apply.md"
    text = apply_md.read_text(encoding="utf-8") if apply_md.exists() else ""
    parsed = parse_apply_md(text)
    basics = {k: str(v or "").strip() for k, v in (master_basics or {}).items()}
    if answers is None:
        from resume_tailor import apply_answers  # lazy: config.py loads .env at import
        answers = apply_answers.load()
    bank = [e for e in (answers or [])
            if isinstance(e, dict) and str(e.get("status", "active")) == "active"]
    sheet_answers = {q: a for q, a in parsed.get("standard_answers", [])}
    values: dict[str, str] = {}

    # Identity: the sheet's Candidate block, the master basics as a fallback.
    candidate = parsed.get("candidate", {})
    for key, label in _CANDIDATE_LABELS.items():
        values[key] = candidate.get(label, "") or basics.get(_BASICS_KEYS[key], "")
    first, last = split_name(values["full_name"])
    values["first_name"], values["last_name"] = first, last

    # Address: the sheet's block, the bank's address_* entries as a fallback.
    address = parsed.get("address", {})
    bank_by_id = {str(e.get("id", "")): e for e in bank}
    for key, label in _ADDRESS_LABELS.items():
        values[key] = address.get(label, "") or _bank_value(bank_by_id.get(key))

    # Standard answers: the sheet's per-job snapshot wins, the bank fills gaps.
    answer_facts: list[Fact] = []
    for entry in bank:
        eid = str(entry.get("id", "")).strip()
        if not eid or eid in _ADDRESS_LABELS:
            continue
        question = str(entry.get("question", "") or "").strip()
        value = sheet_answers.get(question, "") or _bank_value(entry)
        if eid in _NAMED_BANK_IDS:
            values[eid] = value
        else:
            answer_facts.append(Fact(key=f"answer_{eid}", value=value,
                                     description=question or eid.replace("_", " "),
                                     kind="text"))

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
    return FactCatalog(facts + answer_facts, sheet_text=text)


def _bank_value(entry: dict | None) -> str:
    if not entry:
        return ""
    raw = str(entry.get("answer", "") or "").strip()
    if str(entry.get("id", "")) in _BOOL_BANK_IDS and raw:
        return "Yes" if raw.lower() in {"true", "yes", "1"} else "No"
    return raw


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
        years = _YEAR_RE.findall(" ".join(chunks[1:]))
        out["education_grad_year"] = years[-1] if years else ""
        break
    return out


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


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _NORM_RE.sub(" ", (text or "").lower()).split() if t)


def _contains(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def quick_map(label: str, id_or_name: str, type_: str) -> str | None:
    """The fact key for an unmistakable field, else None. Both strings are
    lowercased, non-alphanumerics become spaces, and a phrase has to appear as
    whole tokens (`email` matches "Email address", never "emailing"). `name`
    alone (`name`, `full name`, `your name`) means the full name; `first name`
    and `company name` are their own cases. The identity phrases apply to
    text-like controls only, `resume` / `cv` / `cover letter` to file inputs
    only. An address token (`city`, `state`, `zip`, ...) hits only a text of at
    most `_ADDRESS_MAX_TOKENS` tokens with none of `_ADDRESS_STOP` in it."""
    type_ = (type_ or "").lower()
    for text in (label, id_or_name):
        tokens = _tokens(text)
        if not tokens:
            continue
        for phrase, key, types in _QUICK:
            if type_ in types and _contains(tokens, phrase):
                return key
        if type_ in _TEXTISH and tokens in _NAME_ALONE:
            return "full_name"
        if len(tokens) <= _ADDRESS_MAX_TOKENS and not _ADDRESS_STOP & set(tokens):
            for phrase, key, types in _ADDRESS_QUICK:
                if type_ in types and _contains(tokens, phrase):
                    return key
    return None
