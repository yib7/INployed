"""Hygiene audit for `resume_tailor_files/master_experience.yaml`'s achievement atoms.

    python scripts/atom_audit.py census [--file PATH] [--strict]
    python scripts/atom_audit.py slop   [--file PATH] [--strict]
    python scripts/atom_audit.py gate --old PATH --new PATH

`census` reports what makes tailored bullets repeat themselves or read as noise:
a phrase written into two fields of one atom (the tailor sees `what; how; scope;
impact...` as ONE line, so it gets the fact twice and prints it twice), a phrase
two atoms of the same entry both claim, every six-digit exact count, and any
`+`/`~` figure or smart-punctuation character.

`slop` reads the same atom fields against the avoid-ai-writing skill (v3.18.0,
MIT licence, author Conor Bronsdon), reported under that skill's own P0 / P1 / P2
severity tiers. See "The slop arm" below.

`gate` is the no-new-facts guard for an agent-driven rewrite of those atoms: a
distinctive token (a number, or a word carrying a capital / internal case) that
the new file states and the old one does not is a FABRICATED fact, and the run
stops. It is the atom-layer twin of `local/resume_tailor/verify.py`, which gives
the same guarantee at the bullet layer; the tokenizing rules below are that
module's, re-stated rather than imported because importing anything under
`local/resume_tailor/` runs `config.load_dotenv()` at import scope and a stray
credential load has placed a billed API request before (see `.autopilot/AUTONOMY.md`).

Standard library plus `yaml`. No file is written and no network call is made:
the audit is read-only and free. The one thing loaded from under `local/` is the
pair of files `load_aiwriting()` names, executed by path with no package around
them, so the credential load above still never happens.

── The slop arm ──────────────────────────────────────────────────────────────

Two arms, kept visibly separate in the rule table below.

  * THE VENDORED ARM is `aiwriting.RESUME_EXTRA_BANS`, the very tuples
    `aiwriting.resume_violations()` walks: referenced, never copied, so the atom
    layer and the bullet layer cannot drift into two word lists. It brings
    tier-1 vocabulary, hedging, promotional language, significance inflation,
    vague attribution, formulaic openings and chatbot artifacts, and each of its
    names is mapped onto the skill's severity tier here.
  * THE ATOM ARM is everything the vendored extract deliberately leaves out.
    Read `aiwriting`'s module docstring for why it is bounded: it feeds a REPAIR
    call, so a false positive there rewrites text that was already right. This
    arm only reports, so it can afford the rest of the ruleset: em and en dashes,
    contrast framing in all four of its shapes, adjective stacking, copula
    avoidance, template phrases, transitions, filler, hollow intensifiers,
    engagement hooks, novelty inflation, emotional flatline, speculative
    openers, aphorism formulas, future-narrative closers, meaning-telling,
    cutoff disclaimers, chat citation leaks, AI-tool URL parameters, unfilled
    placeholders, vague third-party validation, the tier-2 cluster rule, and the
    tier-1 words the vendored regex holds back.

REGISTER. An atom is terse technical source material for a bullet, not prose.
The skill's tolerance matrix puts that closest to `docs` / `technical-blog`, so
its own TECHNICAL-BLOG WORD-TABLE EXCEPTION applies verbatim: `robust`,
`comprehensive`, `seamless`, `ecosystem`, `leverage`, `facilitate`, `underpin`
and `streamline` have real technical meanings and are NOT flagged here (the
bullet layer bans them through `compose._STYLE_BANS`, which is a different
register and is not imported). The exception's own carve-outs still fire:
`delve`, `tapestry`, `beacon`, `embark`, `testament to`, `game-changer`, and
`harness` when it is the VERB. `harness` the NOUN is a test rig, and
"a reproducible evaluation harness" is real text in this corpus, so the verb
reading is decided by what sits either side of the word, not by the word.

Four more words are held back for the same reason the vendored docstring holds
them back, since an atom is exactly the register that uses them literally:
`realm` (Keycloak), `paradigm` (programming; the always-slop `paradigm shift` is
matched separately), `landscape` (GIS), `best practices` and `actionable`
(ordinary engineering and monitoring vocabulary), plus `unpack` (unpacking an
archive or a tuple is the literal verb) and `real`/`actual` inflation, whose
carve-out ("real-time streaming", "the actual runtime of a query") needs context
a regex does not have.

SKIPPED, because an atom is a YAML scalar and the rule has nothing to attach to:
hashtag stuffing, emoji in headers, title-case headings, bold overuse,
inline-header lists, list-label periods, bullet-list shape and numbered-list
inflation (no markup, no headings, no list of its own), wall-of-text replies,
social endorsement closers, sycophancy, acknowledgment loops and recap-flattery
(no reader and no conversation), rhetorical-question openers (an atom has no
interrogative form), curly punctuation (the census already counts it), and
subjectless fragments (the register, per `aiwriting.RESUME_PROFILE`).

Also skipped, deliberately, and NOT because the rule is irrelevant: every
structural measure. Sentence- and paragraph-length uniformity, rhythm,
synonym cycling, paragraph-reshuffle immunity, low information density and
type-token ratio all need a piece of prose. Atom fields are not paragraphs and
are not written to be read in sequence, so a rhythm metric over them would
measure the schema, not the writing. Tier-3 density is skipped on the same
ground: a 3%-of-words threshold over a 30-word field fires on one ordinary
"effective". The tier-3 PHRASE table is skipped as well, both of its thresholds
being density measures over a "piece" and its vocabulary being crypto and web3
marketing a technical atom cannot produce. Hedge stacking is not a separate rule
because the vendored `_HEDGE_RE` already matches the first modal of any stack.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from decimal import Decimal, InvalidOperation
from itertools import combinations, count
from pathlib import Path
from types import ModuleType
from typing import (Any, Callable, Dict, Iterable, Iterator, List, NamedTuple,
                    Sequence, Set, Tuple)

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MASTER = REPO / "resume_tailor_files" / "master_experience.yaml"

# The sections that hold achievement atoms. `activities` is not in the file
# today; it is listed so a future section is audited the day it is added.
SECTIONS = ("experience", "projects", "leadership", "activities")

# Entry-level prose that carries figures but is NOT part of any atom. It never
# reaches a bullet, but its numbers are read by a human at interview time and by
# the chat context, so the census reports them for the same conversion pass.
ENTRY_PROSE_KEYS = ("origin", "ship_state", "stack", "context", "schedule")

# Keys that name an entry, in the order `assets.entry_lines` prefers them.
ENTRY_NAME_KEYS = ("name", "org", "title_full", "title", "school")

SHINGLE = 4          # a repeat is a run of this many identical words
FIGURE_MIN = 10_000  # at or above this, a number is rounded to K (design doc)

# ── Figures that are NOT measurements ────────────────────────────────────────
# Anything matched here is invisible to the census's figure list, so keep the
# list short and auditable by eye. These are values whose exact digits ARE the
# fact; rounding one would make it wrong, not readable.
EXCLUDED_FIGURE_PATTERNS = (
    # A bare four-digit year, 1900-2099: "2026" is a date, not a quantity.
    # A dated form like "2026-08-02" tokenizes to 2026 / 08 / 02 and is covered.
    re.compile(r"^(?:19|20)\d\d$"),
    # A dotted version string: v1.4.0, 1.3.3, 3.12 — one figure, never rounded.
    re.compile(r"^\d+(?:\.\d+)+$"),
)
EXCLUDED_FIGURE_LITERALS = frozenset({
    # Embedding width. "1024-dimensional" is a model's fixed shape, and "1K-dim"
    # would be a false statement about the vector store.
    "1024",
})

STYLE_CHARS = (
    ("em dash (U+2014)", "—"),
    ("en dash (U+2013)", "–"),
    ("curly apostrophe (U+2019)", "’"),
    ("curly quote (U+201C)", "“"),
    ("curly quote (U+201D)", "”"),
)

# A figure wearing a `+` floor or a `~` approximation, at ANY magnitude: both
# symbols are banned from the file, so these are reported whatever the value.
# The `+` must sit directly against the digits, which keeps "C++" and a prose
# "3 + 4" out, and the figure must not continue a longer token, which is what
# keeps the `+` in the algorithm name "BM25+rerank" from reading as a floor.
PLUS_FIGURE_RE = re.compile(r"(?<![A-Za-z0-9,.])\d[\d,]*(?:\.\d+)?[KkMmBb]?\+")
TILDE_FIGURE_RE = re.compile(r"~\$?\d[\d,]*(?:\.\d+)?[A-Za-z]*")

# Digit-bearing figures, verify.py's `_NUM_RE`: 40,000 / 3.5 / v1.4.0.
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)*")
# Words, verify.py's `_WORD_RE`: tech names keep their `+`/`#` (C++, C#).
WORD_RE = re.compile(r"[A-Za-z][\w+#]*")
# Shingle tokens: lowercased alphanumeric runs, so "PySide6/Qt" is two words and
# "two-stage" is two words.
TOKEN_RE = re.compile(r"[a-z0-9]+")


# ── Reading the master ───────────────────────────────────────────────────────

class Entry:
    """One job / project / role, with the atoms it owns."""

    def __init__(self, section: str, index: int, raw: Dict[str, Any]) -> None:
        self.section = section
        self.raw = raw
        name = next((str(raw.get(k)).strip() for k in ENTRY_NAME_KEYS
                     if str(raw.get(k) or "").strip()), f"{section}[{index}]")
        self.name = name
        self.label = f"{section}/{name}"
        self.atoms = [a for a in (raw.get("achievements") or []) if isinstance(a, dict)]

    def atom_id(self, atom: Dict[str, Any], index: int) -> str:
        return str(atom.get("id") or f"<atom #{index} has no id>").strip()


def load_master(path: Path) -> Tuple[Dict[str, Any], str]:
    """(parsed master, raw file text). Raises OSError when the file is missing."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not parse to a mapping")
    return data, text


def entries(master: Dict[str, Any]) -> Iterator[Entry]:
    for section in SECTIONS:
        section_entries = master.get(section) or []
        if not isinstance(section_entries, list):
            continue
        for i, raw in enumerate(section_entries):
            if isinstance(raw, dict):
                yield Entry(section, i, raw)


def atom_fields(atom: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The atom's fields the tailor actually sees, labelled and in order.

    This mirrors `local/resume_tailor/assets.py::atom_line()` exactly — joining
    the texts with "; " reproduces its output, which tests/test_atom_audit.py
    pins against the real function. Every other key on the atom (`angles`,
    `hardest_problem`, a relocated sibling key...) is inert: the compose payload
    never carries it, so a repeat living there costs a bullet nothing.
    """
    out: List[Tuple[str, str]] = []
    for key in ("what", "how", "scope"):
        val = str(atom.get(key) or "").strip()
        if val:
            out.append((key, val))
    impact = atom.get("impact")
    if isinstance(impact, (list, tuple)):
        for i, item in enumerate(impact):
            text = str(item or "").strip()
            if text:
                out.append((f"impact[{i}]", text))
    elif str(impact or "").strip():
        out.append(("impact", str(impact).strip()))
    return out


def leaf_strings(val: Any) -> Iterator[str]:
    """Every scalar under `val`, walking dicts and lists.

    `verify.group_source_text` walks atoms the same way, because a master may
    nest a mapping (a `metrics:` block) whose figures are legitimate source.
    """
    if isinstance(val, dict):
        for key, item in val.items():
            if not str(key).startswith("_"):
                yield from leaf_strings(item)
    elif isinstance(val, (list, tuple, set)):
        for item in val:
            yield from leaf_strings(item)
    elif val is not None and not isinstance(val, bool):
        yield str(val)


# ── Repeated phrases ─────────────────────────────────────────────────────────

_separator = count()


def words(text: str) -> List[str]:
    return TOKEN_RE.findall(text.lower())


def _sequence(fields: Sequence[Tuple[str, str]]) -> List[str]:
    """One atom's fields as a single token list, with a unique separator between
    them so no reported phrase can straddle a field boundary."""
    seq: List[str] = []
    for _, text in fields:
        seq.append(f"\x00{next(_separator)}")
        seq.extend(words(text))
    return seq


def common_runs(a: Sequence[str], b: Sequence[str],
                min_len: int = SHINGLE) -> Set[Tuple[str, ...]]:
    """Every maximal run of `min_len`+ identical words shared by `a` and `b`.

    Maximal rather than every shingle: one repeated sentence is one finding, not
    the dozen overlapping 4-grams it contains (the "collapse substrings" rule).
    """
    runs: Set[Tuple[str, ...]] = set()
    if not a or not b:
        return runs
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] != b[j - 1]:
                continue
            cur[j] = prev[j - 1] + 1
            # right-maximal: the run cannot be extended by one more word
            if cur[j] >= min_len and (i == len(a) or j == len(b) or a[i] != b[j]):
                runs.add(tuple(a[i - cur[j]:i]))
        prev = cur
    return runs


def _contains(big: Tuple[str, ...], small: Tuple[str, ...]) -> bool:
    n = len(small)
    return n < len(big) and any(big[i:i + n] == small for i in range(len(big) - n + 1))


def collapse(found: Dict[Tuple[str, ...], Set[str]]) -> List[Tuple[Tuple[str, ...], Set[str]]]:
    """Drop a phrase that sits inside a longer one covering the same places."""
    kept: List[Tuple[Tuple[str, ...], Set[str]]] = []
    for phrase, places in sorted(found.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        if any(places <= seen and _contains(longer, phrase) for longer, seen in kept):
            continue
        kept.append((phrase, places))
    return kept


def intra_atom_repeats(entry: Entry) -> List[Tuple[str, str, str]]:
    """(atom id, "field + field", phrase) for a phrase in two fields of one atom."""
    out: List[Tuple[str, str, str]] = []
    for i, atom in enumerate(entry.atoms):
        fields = [(label, words(text)) for label, text in atom_fields(atom)]
        found: Dict[Tuple[str, ...], Set[str]] = {}
        for (l1, t1), (l2, t2) in combinations(fields, 2):
            for run in common_runs(t1, t2):
                found.setdefault(run, set()).update({l1, l2})
        for phrase, places in collapse(found):
            out.append((entry.atom_id(atom, i), " + ".join(sorted(places)), " ".join(phrase)))
    return sorted(out)


def cross_atom_repeats(entry: Entry) -> List[Tuple[str, str]]:
    """("atom id + atom id", phrase) for a phrase two atoms of this entry share."""
    seqs = [(entry.atom_id(atom, i), _sequence(atom_fields(atom)))
            for i, atom in enumerate(entry.atoms)]
    found: Dict[Tuple[str, ...], Set[str]] = {}
    for (id1, s1), (id2, s2) in combinations(seqs, 2):
        for run in common_runs(s1, s2):
            found.setdefault(run, set()).update({id1, id2})
    return sorted((" + ".join(sorted(ids)), " ".join(phrase))
                  for phrase, ids in collapse(found))


# ── Figures ──────────────────────────────────────────────────────────────────

def is_reportable_figure(token: str) -> bool:
    """True for an integer at or above FIGURE_MIN that is a real measurement."""
    norm = token.replace(",", "")
    if norm in EXCLUDED_FIGURE_LITERALS or token in EXCLUDED_FIGURE_LITERALS:
        return False
    if any(p.match(norm) for p in EXCLUDED_FIGURE_PATTERNS):
        return False
    if not norm.isdigit():
        return False
    return int(norm) >= FIGURE_MIN


def figures_in(text: str) -> List[str]:
    out: List[str] = []
    for match in NUM_RE.finditer(text or ""):
        token = match.group().rstrip(",.")
        if is_reportable_figure(token):
            out.append(token)
    return out


def symbol_figures(raw_text: str) -> List[Tuple[int, str]]:
    """(line number, matched text) for every `+`-floored or `~`-approximated
    figure anywhere in the file, at any magnitude."""
    out: List[Tuple[int, str]] = []
    for lineno, line in enumerate(raw_text.splitlines(), start=1):
        for regex in (PLUS_FIGURE_RE, TILDE_FIGURE_RE):
            for match in regex.finditer(line):
                out.append((lineno, match.group().strip()))
    return out


# ── The no-new-facts gate ────────────────────────────────────────────────────
#
# The rules below are `local/resume_tailor/verify.py`'s, narrowed to what the
# atom layer needs; read that module's docstring for the full rationale and the
# holes it deliberately leaves. Differences, both deliberate:
#   * No sentence-initial free pass. That slot exists in verify because it holds
#     a GENERATED action verb; an atom field is a fragment the user wrote, and
#     capitalizing a word that already exists in the old file grounds anyway
#     (matching is case-insensitive).
#   * The OLD side's source is every leaf scalar of every atom — inert sibling
#     keys included — plus the entry names, exactly as `group_source_text(ids,
#     extra=_entry_names(...))` does. This phase RELOCATES true detail into
#     sibling keys, and a fact already written in the file is not a new fact.

# The word that replaces a banned `+` floor ("100+ customers" -> "over 100
# customers"). Lowercase, so it is not distinctive anyway; named here so the
# allowance is visible to a reader of this file.
ALLOWED_NEW_WORDS = frozenset({"over"})

SUFFIX_MULTIPLIERS = {"k": Decimal(1_000), "m": Decimal(1_000_000),
                      "b": Decimal(1_000_000_000),
                      "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000)}
# A figure with a magnitude suffix, on lowercased text: 512k / 1.2 million.
SUFFIXED_RE = re.compile(
    r"(?<![0-9a-z.])(\d+(?:\.\d+)?)\s?(million|billion|k|m|b)(?![0-9a-z])")


def norm_source(source: str) -> str:
    """Lowercased, with thousands-commas stripped so 40,000 == 40000."""
    return re.sub(r"(?<=\d),(?=\d)", "", (source or "").lower())


def distinctive_word(token: str) -> bool:
    """Worth tracing: two characters or more, carrying a capital (SQL, PySide6)."""
    return len(token) >= 2 and any(c.isupper() for c in token)


def word_grounded(token: str, source: str) -> bool:
    """`token` appears in `source` aligned to a word boundary on at least one
    side — "SQL" is grounded by "PostgreSQL", "MIT" is not by "committed"."""
    esc = re.escape(token)
    return bool(re.search(rf"(?<![0-9a-z]){esc}", source)
                or re.search(rf"{esc}(?![0-9a-z])", source))


def plural_grounded(token: str, source: str) -> bool:
    """A written plural is grounded by its own singular ("APIs" by "API")."""
    if not token.endswith("s") or token.endswith("ss"):
        return False
    stem = token[:-1].lower()
    if len(stem) < 2:
        return False
    if len(stem) == 2:
        esc = re.escape(stem)
        return bool(re.search(rf"(?<![0-9a-z]){esc}(?![0-9a-z])", source))
    return word_grounded(stem, source)


def num_grounded(token: str, source: str) -> bool:
    """A number must match on its own digit boundaries: "40" is NOT grounded by
    "40,000", because a different figure is a different claim. A figure the old
    file abbreviates grounds its long form ("100,000" by "100K")."""
    norm = token.replace(",", "").rstrip(".")
    if not norm:
        return True
    if re.search(rf"(?<![\d.]){re.escape(norm)}(?![\d])", source):
        return True
    try:
        value = Decimal(norm)
    except InvalidOperation:
        return False
    return any(v == value for v in suffixed_values(source))


def suffixed_values(source: str) -> Iterator[Decimal]:
    for digits, suffix in SUFFIXED_RE.findall(source):
        try:
            yield Decimal(digits) * SUFFIX_MULTIPLIERS[suffix]
        except InvalidOperation:  # pragma: no cover - the regex yields parseable digits
            continue


def source_numbers(source: str) -> List[Decimal]:
    """Every figure the old file states, suffixed forms expanded."""
    out: List[Decimal] = []
    for match in NUM_RE.finditer(source):
        token = match.group().rstrip(",.").replace(",", "")
        try:
            out.append(Decimal(token))
        except InvalidOperation:
            continue
    out.extend(suffixed_values(source))
    return out


def rounding_targets(text: str) -> Dict[str, List[Tuple[Decimal, Decimal]]]:
    """digits -> [(value, precision step)] for each suffixed figure in `text`.

    "512K" is (512000, 1000): it claims the old figure to the nearest thousand.
    "1.2 million" is (1200000, 100000) — one decimal place of a million.
    """
    out: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
    for digits, suffix in SUFFIXED_RE.findall(text.lower()):
        try:
            mult = SUFFIX_MULTIPLIERS[suffix]
            value = Decimal(digits) * mult
        except InvalidOperation:  # pragma: no cover
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.setdefault(digits, []).append((value, mult / (10 ** decimals)))
    return out


def rounds_to_an_old_figure(token: str, rounded: Dict[str, List[Tuple[Decimal, Decimal]]],
                            old_numbers: Sequence[Decimal]) -> bool:
    """True when the new text writes this token as a K/million abbreviation of a
    figure the old file states: 512,384 -> 512K, 70,000 -> 70K, ~21k -> 21K."""
    for value, step in rounded.get(token.replace(",", ""), ()):
        half = step / 2
        if any(abs(old - value) <= half for old in old_numbers):
            return True
    return False


def gate_source(master: Dict[str, Any]) -> str:
    """Everything the old file already says: every atom scalar plus entry names."""
    parts: List[str] = []
    for entry in entries(master):
        parts.extend(str(entry.raw.get(k) or "") for k in ENTRY_NAME_KEYS)
        for atom in entry.atoms:
            for key, val in atom.items():
                if not str(key).startswith("_"):
                    parts.extend(leaf_strings(val))
    return "\n".join(p for p in parts if p)


def new_facts(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[List[Tuple[str, str, str]], int, int]:
    """(offenders, tokens checked, atoms checked).

    An offender is (location, kind, token) for a distinctive token the new atoms
    state and the old file does not, after the declared conversions.
    """
    source = norm_source(gate_source(old))
    old_numbers = source_numbers(source)
    offenders: List[Tuple[str, str, str]] = []
    checked = atoms = 0
    for entry in entries(new):
        for i, atom in enumerate(entry.atoms):
            atoms += 1
            where = f"{entry.label} :: {entry.atom_id(atom, i)}"
            for label, text in atom_fields(atom):
                rounded = rounding_targets(text)
                for token in NUM_RE.findall(text):
                    checked += 1
                    if num_grounded(token, source):
                        continue
                    if rounds_to_an_old_figure(token, rounded, old_numbers):
                        continue
                    offenders.append((f"{where} / {label}", "number", token))
                for token in WORD_RE.findall(text):
                    if not distinctive_word(token):
                        continue
                    checked += 1
                    low = token.lower()
                    if low in ALLOWED_NEW_WORDS or word_grounded(low, source):
                        continue
                    if plural_grounded(token, source):
                        continue
                    offenders.append((f"{where} / {label}", "word", token))
    return offenders, checked, atoms


# ── The slop arm ─────────────────────────────────────────────────────────────
#
# The rule table is at the bottom of this section; read the module docstring for
# the register argument, the technical carve-outs and the skipped rules.

# The skill's three severity tiers, with the headings it gives them.
P0, P1, P2 = "P0", "P1", "P2"
TIER_TITLES = {P0: "credibility killers",
               P1: "obvious AI smell",
               P2: "stylistic polish"}

# A synthetic parent package for the two files `load_aiwriting` executes. The
# name is deliberately not `resume_tailor`, so nothing here can shadow the real
# package for another importer in the same process (pytest imports it for real).
_AIWRITING_PKG = "_atom_audit_aiwriting"


def load_aiwriting() -> ModuleType:
    """`local/resume_tailor/aiwriting.py`, loaded WITHOUT the package around it.

    The module docstring's rule holds: importing `local.resume_tailor` runs
    `config.load_dotenv()` at import scope, and a stray credential load has
    placed a billed API request before. So the two files this arm needs are
    executed by path under the synthetic parent above: `common.py`, which is
    stdlib-only, and `aiwriting.py`, which imports it and nothing else.
    `resume_tailor/__init__.py` never runs, `config.py` is never read, and no
    other module of the engine is loaded. tests/test_atom_slop.py pins that.
    """
    loaded = sys.modules.get(f"{_AIWRITING_PKG}.aiwriting")
    if loaded is not None:
        return loaded
    base = REPO / "local" / "resume_tailor"
    pkg = ModuleType(_AIWRITING_PKG)
    pkg.__path__ = [str(base)]          # type: ignore[attr-defined]
    sys.modules[_AIWRITING_PKG] = pkg
    for name in ("common", "aiwriting"):
        source = base / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"{_AIWRITING_PKG}.{name}", source)
        if spec is None or spec.loader is None:
            raise OSError(f"cannot load {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{_AIWRITING_PKG}.aiwriting"]


class Rule(NamedTuple):
    """One check: its severity tier, its report name, and what it finds.

    `find` returns the offending spans as they appear in the field, so the
    report can quote the text rather than name a category at it.
    """
    tier: str
    name: str
    find: Callable[[str], List[str]]


def _matches(pattern: re.Pattern) -> Callable[[str], List[str]]:
    return lambda text: [m.group(0).strip() for m in pattern.finditer(text)]


def _rule(tier: str, name: str, pattern: str, flags: int = re.I) -> Rule:
    return Rule(tier, name, _matches(re.compile(pattern, flags)))


# ── Word-level context, shared by the two rules that need to read a POS ──────
_PREV_WORD_RE = re.compile(r"[\w'’-]+\s*$")
_NEXT_WORD_RE = re.compile(r"^\s*([\w'’-]+)")

# A determiner or possessive directly before the word makes it the head of a
# noun phrase ("a harness the team built", "the features a user sees").
_DETERMINERS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "its", "their", "our",
    "my", "your", "his", "her", "no", "each", "every", "one", "two", "three",
    "several", "many", "all", "some", "other", "same", "such", "key", "new",
    "core", "main", "extra", "few", "both", "which", "what",
})
# A determiner or possessive directly AFTER the word means the word took an
# object, which is the verb reading.
_OBJECTS = frozenset({"a", "an", "the", "its", "their", "our", "this",
                      "these", "those", "all", "full", "every", "my", "your"})


def _prev_word(text: str, at: int) -> str:
    match = _PREV_WORD_RE.search(text[:at])
    return match.group(0).strip().lower() if match else ""


def _next_word(text: str, at: int) -> str:
    match = _NEXT_WORD_RE.match(text[at:])
    return match.group(1).lower() if match else ""


def _takes_an_object(text: str, start: int, end: int) -> bool:
    """True when the word at [start:end) reads as a verb with an object.

    The test is positional, because the word alone cannot settle it. Two
    conditions, and they do different work:

      * RIGHT (the one that carries most cases): a verb takes an object, so a
        determiner must follow. "harness the power of" and "features a dark
        mode" pass; "a reproducible evaluation harness that drove ..." and "the
        evaluation harness runs ..." do not, because `that` and `runs` are not
        determiners. That alone settles every `harness` in the live corpus.
      * LEFT: a determiner directly before the word makes the word the head of
        its own noun phrase, whatever follows. This is what stops "wrote a
        harness the reviewers run" and "the features a user sees".
    """
    return (_prev_word(text, start) not in _DETERMINERS
            and _next_word(text, end) in _OBJECTS)


# ── harness: the tier-2 verb, never the test rig ─────────────────────────────
# The skill's technical-blog exception keeps `harness` flaggable while letting
# `robust` and `leverage` through, so the word cannot simply be dropped. The
# NOUN is a real thing in this corpus ("a reproducible evaluation harness" is a
# test rig, written by the user), and a flat word-list match calls it slop.
_HARNESS_RE = re.compile(r"\bharness(?:es|ed|ing)?\b", re.I)


def harness_as_a_verb(text: str) -> List[str]:
    """Spans where `harness` is the tier-2 verb meaning "use".

    Known and accepted gap: an object with no determiner ("harness GPU memory")
    is missed. Widening to bare nouns would take "evaluation harness runs" with
    it, and a missed P1 word costs a report line while a false positive costs
    the corpus's trust in the whole report.
    """
    out: List[str] = []
    for match in _HARNESS_RE.finditer(text):
        if _takes_an_object(text, match.start(), match.end()):
            tail = _NEXT_WORD_RE.match(text[match.end():])
            out.append(f"{match.group(0)} {tail.group(1)}" if tail else match.group(0))
    return out


# ── Copula avoidance ─────────────────────────────────────────────────────────
# "serves as" has no honest reading, so it needs no context test. `features`,
# `presents` and `represents` are each a common NOUN or a legitimate technical
# verb in this corpus ("a slew of search features", "the app presents a form"),
# so they go through the same positional test as `harness`.
_SERVES_AS_RE = re.compile(r"\b(?:serves?|serving)\s+as\b", re.I)
_COPULA_VERB_RE = re.compile(r"\b(?:features|featuring|presents|represents)\b", re.I)


def copula_avoidance(text: str) -> List[str]:
    out = [m.group(0) for m in _SERVES_AS_RE.finditer(text)]
    for match in _COPULA_VERB_RE.finditer(text):
        if _takes_an_object(text, match.start(), match.end()):
            tail = _NEXT_WORD_RE.match(text[match.end():])
            out.append(f"{match.group(0)} {tail.group(1)}" if tail else match.group(0))
    return out


# ── Contrast framing, in all four shapes the skill names ─────────────────────
# Bare "rather than" and "instead of" are NOT here, and that is the single
# biggest difference from `compose._STYLE_BANS`. In a bullet they are the tell;
# in an atom they are how the user records an implementation choice ("key the
# cache by job id rather than title, because two postings can share a title").
# Used that way, each one records a real decision. What is
# banned is the REVEAL: a negation whose only job is to set up the correction
# that follows.
_NEG = (r"(?:(?:is|was|are|were|does|do|did|has|have)\s*n(?:'|’)?t"
        r"|(?:it|this|that|there)(?:'|’)s\s+not"
        r"|\b(?:is|was|are|were)\s+not)")
_REVEAL = r"(?:it|this|that)(?:(?:'|’)s|\s+is|\s+was)"

# "The headline is not the speed, it's the tooling." One sentence, one pivot.
_CONTRAST_JOINED_RE = re.compile(
    _NEG + r"[^.;!?]{0,80}?[,;]\s*" + _REVEAL + r"\b", re.I)
# "not X but Y", and the bare "not just" intensifier the same move leans on.
_CONTRAST_BUT_RE = re.compile(
    r"\bnot\s+(?:just\s+|only\s+|merely\s+|simply\s+)?[^.;!?]{1,60}?\bbut\s+"
    r"(?:rather\s+|also\s+)?\w+"
    r"|\bnot\s+(?:just|only|merely|simply)\b", re.I)
# The split-sentence form: the negation and the correction in two sentences, each
# of which reads as an innocent declarative on its own.
_SPLIT_NEG_RE = re.compile(_NEG, re.I)
_SPLIT_REVEAL_RE = re.compile(
    r"^\s*(?:the|what)\s+(?:real|actual|true|bigger|whole|interesting)\s+\w+"
    r"|^\s*" + _REVEAL + r"\s+(?:really\s+)?(?:about|the)\b", re.I)
# The multi-negation countdown: two sentences in a row that open on a negation.
_COUNTDOWN_RE = re.compile(r"^\s*(?:" + _REVEAL + r"\s+)?not\b", re.I)
# The tailing negation: a bare negated fragment stapled to the end of a claim.
# Capped at two words, so a real clause (", no rows were dropped") is not one.
_TAIL_NEG_RE = re.compile(r",\s+no\s+[\w-]+(?:\s+[\w-]+)?\s*[.;!?]?\s*$", re.I)
# The skill's carve-out: a LIST of spec constraints ("no dependencies, no
# telemetry") is list content, not a reveal.
_NO_ITEM_RE = re.compile(r"\bno\s+[\w-]+", re.I)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+")


def contrast_framing(text: str) -> List[str]:
    """Spans of "it's not X, it's Y" and its three quieter shapes."""
    out: List[str] = []
    out.extend(m.group(0) for m in _CONTRAST_JOINED_RE.finditer(text))
    out.extend(m.group(0) for m in _CONTRAST_BUT_RE.finditer(text))
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    for first, second in zip(sentences, sentences[1:]):
        if _SPLIT_NEG_RE.search(first) and _SPLIT_REVEAL_RE.search(second):
            out.append(f"{first.strip()} {second.strip()}")
        elif _COUNTDOWN_RE.search(first) and _COUNTDOWN_RE.search(second):
            out.append(f"{first.strip()} {second.strip()}")
    for sentence in sentences:
        match = _TAIL_NEG_RE.search(sentence)
        if match and len(_NO_ITEM_RE.findall(sentence)) < 2:
            out.append(match.group(0).strip())
    return out


# ── Tier 2: legitimate alone, an AI signal in pairs ──────────────────────────
# The skill's own threshold, kept: two or more in one field. Absent by design are
# the eight technical-blog exception words, `navigate` (navigating a tree is the
# literal verb), `augment` (data augmentation), and `deeply` / `quietly`, which
# the skill itself limits to significance collocations a regex cannot separate
# from "deeply nested". `harness` has its own rule above.
_TIER2_RE = re.compile(
    r"\b(?:foster|elevat|unleash|empower|bolster|spearhead|resonat|revolutioniz"
    r"|nuanced|crucial|multifaceted|myriad|plethora|encompass|catalyz|reimagin"
    r"|galvaniz|cultivat|illuminat|elucidat|juxtapos|transformative|cornerstone"
    r"|paramount|burgeoning|nascent|quintessential|overarching)\w*", re.I)


def tier2_cluster(text: str) -> List[str]:
    """[the cluster] when two or more DISTINCT tier-2 words share one field."""
    found = {m.group(0).lower(): m.group(0) for m in _TIER2_RE.finditer(text)}
    if len(found) < 2:
        return []
    return [", ".join(sorted(found.values()))]


# ── Adjective stacking ───────────────────────────────────────────────────────
# Two shapes, both of which the user bans outright in atom text (see the resume
# writing-style note): hyphenated compound modifiers piled on one noun, and the
# skill's "compulsive rule of three" as an adjective train.
#
# THREE modifiers, not two, and this threshold was calibrated against the live
# corpus, which a two-modifier rule reported six times and was wrong six times.
# The skill's own example is a pile-up ("a high-quality, well-architected,
# future-proof solution") and the tell is the pile, not the hyphen. Two
# hyphenated compounds are ordinary precise English here: a "single-page,
# offline viewer" names two distinct architectural facts, and
# "size-limit and bad-input rejections" or "security-clearance and
# advanced-degree filters" are coordinated compound NOUNS, not modifiers at all.
# An `and` pair is therefore out entirely; only a comma pile-up of three counts.
#
# `_ADJ` carries no `-ed` / `-ing` suffix for the same reason: "crawled, parsed,
# and indexed every page" is a factual enumeration of steps, not three
# adjectives, and an atom is where those enumerations belong.
_ADJ = r"\w+(?:ive|ous|ful|able|ible|ical|istic|ent|ant|ary|less)"
_HYPHEN_STACK = r"\b[a-z]+-[a-z]+,\s+[a-z]+-[a-z]+,?\s+(?:and\s+)?[a-z]+-[a-z]+\s+[a-z]+\b"
_ADJ_TRAIN = (r"\b" + _ADJ + r",\s+" + _ADJ + r",?\s+(?:and\s+)?" + _ADJ + r"\s+\w+")


SLOP_RULES: Tuple[Rule, ...] = (
    # ── P0: credibility killers ──────────────────────────────────────────────
    _rule(P0, "cutoff disclaimer",
          r"\bas of my (?:last |latest )?(?:update|knowledge|training)\b"
          r"|\bknowledge cut[- ]?off\b"
          r"|\bbased on (?:the )?(?:available|limited) information\b"
          r"|\b(?:I do not|I don't|I cannot|I can't) (?:have access to|access)\b"
          r"|\bwhile specific details are limited\b"),
    _rule(P0, "chat citation leak",
          r"cite\w*turn\d+\w*|contentReference\[oaicite|oai_citation"
          r"|\[attached_file:\d|grok_card"),
    _rule(P0, "ai-tool url parameter",
          r"utm_source=(?:chatgpt\.com|copilot\.com|openai|claude\.ai|perplexity\.ai)"
          r"|referrer=grok\.com"),
    _rule(P0, "unfilled placeholder",
          r"\[(?:your|insert|add|enter|describe|specify|choose)\b[^\]]*\]"
          r"|\b\d{4}-XX-XX\b"
          r"|<!--\s*(?:add|fill in|todo|insert)\b"),
    # The inverse of the vendored "vague attribution": there the authority has a
    # generic name ("studies show", "analysts agree", all of which
    # `_VAGUE_ATTRIBUTION_RE` already matches and none of which is repeated
    # here); here it is deliberately withheld, which is harder to check and
    # easier to invent.
    _rule(P0, "vague third-party validation",
          r"\b(?:independent|third[- ]party|an outside)\s+"
          r"(?:testing|tests?|benchmarks?|analysis|audits?|party|parties)\s+"
          r"(?:\w+\s+){0,2}?(?:confirms?|shows?|proves?|proved|validates?|puts?|found)\b"),

    # ── P1: obvious AI smell ─────────────────────────────────────────────────
    # The user's standing ban, target zero. The census counts these too; here
    # they are quoted in place, next to whatever else the field is doing wrong.
    _rule(P1, "em or en dash", r"[—–]|\s--\s", 0),
    Rule(P1, "contrast framing", contrast_framing),
    Rule(P1, "harness as a verb", harness_as_a_verb),
    # The tier-1 entries the vendored regex holds back but the technical-blog
    # exception still flags, plus the ones with no technical reading at all. The
    # words that ARE exempt here are named in the module docstring.
    _rule(P1, "tier-1 vocabulary (atom arm)",
          r"\btapestry\b|\bbeacons?\b|\bembark(?:s|ed|ing)?\b"
          r"|\bgame[- ]?chang(?:er|ers|ing)\b|\butiliz(?:e|es|ed|ing|ation)\b"
          r"|\bcutting[- ]edge\b|\bdeep dive\b|\bholistic(?:ally)?\b"
          r"|\bcommenc(?:e|es|ed|ing)\b|\bascertain(?:s|ed|ing)?\b"
          r"|\bendeavou?rs?\b|\bhits? different(?:ly)?\b"
          r"|\bthe future looks bright\b|\bonly time will tell\b"
          r"|\bunderscor(?:es|ed|ing)\s+(?:the|a|an|how|why|that|its|their)\b"),
    _rule(P1, "hollow intensifier",
          r"\btruly\b|\bgenuinely\b|\bquite frankly\b|\bto be honest\b"
          r"|\blet(?:'|’)s be clear\b"
          r"|\bworth (?:reading|a look|exploring|checking out|your time|paying attention to)\b"),
    _rule(P1, "template phrase",
          r"\ba \w+ step (?:towards?|forward)\b"
          r"|\bwhether you(?:'|’)?re\b|\bwhether you are\b"
          r"|\bI recently had the pleasure of\b"
          r"|\bdesigned for long[- ]term\b"
          r"|\bplayed a (?:\w+ )?role in\b"
          r"|\bcontributed to [\w\s]{0,20}initiatives\b"
          r"|\bworked closely with cross[- ]functional\b"
          r"|\bdelivered high[- ]quality results\b"),
    _rule(P1, "adjective stacking", _HYPHEN_STACK + "|" + _ADJ_TRAIN),
    # The field-initial "Let's <verb>" is here and not left to the vendored
    # chatbot rule on purpose. `_CHATBOT_RE` ends its alternation
    # `(?:certainly|...|let's\s+\w)\b`, and that trailing boundary applies to the
    # whole group, so it can only match a one-letter verb; "Let's walk through
    # the exporter" never fires there. Recorded in `.autopilot/BACKLOG.md`
    # rather than fixed, because the fix changes what the BULLET layer repairs.
    _rule(P1, "engagement hook",
          r"(?:^|[.;!?]\s+)(?:the (?:catch|kicker|result|best part|twist))\s*[?:]"
          r"|(?:^|[.;!?]\s+)here(?:'|’)s the (?:thing|kicker|interesting part)\b"
          r"|(?:^|[.;!?]\s+)plot twist\s*[:.]"
          r"|(?:^|[.;!?]\s+)(?:honestly|look|real talk)\s*[,:?]"
          r"|(?:^|[.;!?]\s+)let(?:'|’)s\s+\w"),
    _rule(P1, "novelty inflation",
          r"\bnobody (?:talks about|is naming|(?:is )?nam(?:ing|es))\b"
          r"|\bno one (?:talks about|is naming)\b"
          r"|\bthe insight everyone(?:'|’)s missing\b"
          r"|\bwhat nobody tells you\b|\bcoined the (?:term|phrase)\b"
          r"|\ba (?:problem|failure mode) (?:nobody|no one)\b"),
    _rule(P1, "emotional flatline",
          r"\bwhat (?:surprised|struck|fascinated) me\b"
          r"|\bI was (?:fascinated|excited|amazed|thrilled) to\b"
          r"|\bthe most interesting (?:part|thing|bit)\b"
          r"|\binteresting (?:part|thing|aspect) (?:of|here|about)\b"),
    _rule(P1, "speculative opener",
          r"\b(?:imagine|picture|envision) a (?:world|future|system) (?:where|in which)\b"),
    _rule(P1, "aphorism formula",
          r"\bis the (?:language|currency|lifeblood|bedrock) of\b"
          r"|\bis not a \w+ but a \w+\b"),
    _rule(P1, "future-narrative closer",
          r"\b(?:may|might|could|will|is poised to)\s+become\s+"
          r"(?:one of )?the (?:most|next|defining)\b"),
    _rule(P1, "meaning-telling",
          r"\b(?:represents|symboliz(?:es|ing)|signals|speaks to)\s+a\s+"
          r"(?:broader|larger|wider|new|fundamental)\b"
          r"|\bspeaks to a larger trend\b"),
    Rule(P1, "tier-2 cluster", tier2_cluster),

    # ── P2: stylistic polish ─────────────────────────────────────────────────
    Rule(P2, "copula avoidance", copula_avoidance),
    _rule(P2, "transition phrase",
          r"(?:^|[.;!?]\s+)(?:moreover|furthermore|additionally|in conclusion"
          r"|in summary|to summarize|that said|that being said)\b"
          r"|\bwhen it comes to\b|\bat the end of the day\b"),
    _rule(P2, "filler phrase",
          r"\bit is important to note that\b|\bit(?:'|’)s important to note\b"
          r"|\bin terms of\b|\bthe reality is that\b|\bneedless to say\b"),
    _rule(P2, "generic conclusion",
          r"\bone thing is certain\b|\bas we move forward\b"
          r"|\bdespite (?:the )?challenges\b[^.]{0,40}\bthrive\b"),
)


def slop_findings(text: str, vendored: Sequence[Tuple[str, str, re.Pattern]]
                  ) -> List[Tuple[str, str, str]]:
    """(tier, rule name, quoted span) for every AI-writing hit in one field."""
    out: List[Tuple[str, str, str]] = []
    for tier, name, pattern in vendored:
        out.extend((tier, name, _quote(m.group(0))) for m in pattern.finditer(text))
    for rule in SLOP_RULES:
        out.extend((rule.tier, rule.name, _quote(span)) for span in rule.find(text))
    return out


# How the vendored ban names map onto the skill's severity tiers. Every name in
# `aiwriting.RESUME_EXTRA_BANS` must appear here; `vendored_rules` fails loudly
# if the bullet layer adds one, because an unmapped ban would silently stop
# being audited at the atom layer.
VENDORED_TIERS = {
    "chatbot artifact": P0,
    "vague attribution": P0,
    "significance inflation": P0,
    "tier-1 vocabulary": P1,
    "hedge": P1,
    "promotional language": P1,
    "formulaic opening": P1,
}


def vendored_rules() -> List[Tuple[str, str, re.Pattern]]:
    """(tier, name, pattern) for `aiwriting.RESUME_EXTRA_BANS`, in its own order.

    The compiled patterns are the ones `resume_violations()` walks, referenced
    rather than copied, so the two layers share one word list.
    """
    bans = load_aiwriting().RESUME_EXTRA_BANS
    unmapped = [name for name, _ in bans if name not in VENDORED_TIERS]
    if unmapped:
        raise ValueError("aiwriting.RESUME_EXTRA_BANS has bans this audit does "
                         f"not tier: {', '.join(unmapped)}")
    return [(VENDORED_TIERS[name], name, pattern) for name, pattern in bans]


_WHITESPACE_RE = re.compile(r"\s+")
QUOTE_CAP = 90


def _quote(span: str) -> str:
    """One offending span on one line, bounded so a long sentence stays readable."""
    flat = _WHITESPACE_RE.sub(" ", span).strip()
    return flat if len(flat) <= QUOTE_CAP else flat[:QUOTE_CAP - 3] + "..."


# ── Reports ──────────────────────────────────────────────────────────────────

def _ascii(text: str) -> str:
    """The report is printed on a cp1252 console; keep it to characters it has."""
    return str(text).encode("ascii", "replace").decode("ascii")


def run_census(path: Path, strict: bool) -> int:
    master, raw = load_master(path)
    intra: List[Tuple[str, str, str, str]] = []
    cross: List[Tuple[str, str, str]] = []
    figures: List[Tuple[str, str]] = []
    for entry in entries(master):
        for atom_id, fields, phrase in intra_atom_repeats(entry):
            intra.append((entry.label, atom_id, fields, phrase))
        for ids, phrase in cross_atom_repeats(entry):
            cross.append((entry.label, ids, phrase))
        for i, atom in enumerate(entry.atoms):
            for label, text in atom_fields(atom):
                for fig in figures_in(text):
                    figures.append((f"{entry.label} :: {entry.atom_id(atom, i)} / {label}", fig))
        for key in ENTRY_PROSE_KEYS:
            for text in leaf_strings(entry.raw.get(key)):
                for fig in figures_in(text):
                    figures.append((f"{entry.label} :: entry.{key}", fig))
    symbols = symbol_figures(raw)
    style = [(name, raw.count(char)) for name, char in STYLE_CHARS]

    print(f"atom audit census: {_ascii(path)}")
    print()
    print(f"INTRA-ATOM REPEATS: {len(intra)}"
          f"   (a {SHINGLE}-word phrase in two fields of one atom)")
    for label, atom_id, fields, phrase in intra:
        print(f"  {_ascii(label)} :: {_ascii(atom_id)}")
        print(f"      {_ascii(fields)}: {_ascii(phrase)}")
    print()
    print(f"CROSS-ATOM REPEATS: {len(cross)}"
          f"   (a {SHINGLE}-word phrase two atoms of one entry share)")
    for label, ids, phrase in cross:
        print(f"  {_ascii(label)}")
        print(f"      {_ascii(ids)}: {_ascii(phrase)}")
    print()
    print(f"FIGURES: {len(figures)}"
          f"   (integers >= {FIGURE_MIN:,}, years/versions/1024 excluded)")
    for where, fig in figures:
        print(f"  {_ascii(where)}: {fig}")
    print()
    print(f"FIGURES WITH + OR ~: {len(symbols)}   (any magnitude, anywhere in the file)")
    for lineno, text in symbols:
        print(f"  line {lineno}: {_ascii(text)}")
    print()
    print("STYLE   (baseline is 0 for every row)")
    for name, n in style:
        print(f"  {name}: {n}")
    print()
    total = len(intra) + len(cross) + len(figures) + len(symbols) + sum(n for _, n in style)
    print(f"TOTAL FINDINGS: {total}")
    if strict and total:
        print("strict: findings outstanding")
        return 1
    return 0


def run_slop(path: Path, strict: bool) -> int:
    master, _ = load_master(path)
    vendored = vendored_rules()
    found: Dict[str, List[Tuple[str, str, str]]] = {P0: [], P1: [], P2: []}
    atoms = fields = 0
    for entry in entries(master):
        for i, atom in enumerate(entry.atoms):
            atoms += 1
            where = f"{entry.label} :: {entry.atom_id(atom, i)}"
            for label, text in atom_fields(atom):
                fields += 1
                for tier, name, span in slop_findings(text, vendored):
                    found[tier].append((f"{where} / {label}", name, span))

    print(f"atom audit slop: {_ascii(path)}")
    print("  ruleset: avoid-ai-writing v3.18.0 (MIT, Conor Bronsdon), atom profile")
    print(f"  scanned: {fields} fields across {atoms} atoms")
    print()
    for tier in (P0, P1, P2):
        rows = found[tier]
        print(f"{tier}  {TIER_TITLES[tier].upper()}: {len(rows)}")
        for where, name, span in rows:
            print(f"  {_ascii(where)}")
            print(f"      {_ascii(name)}: {_ascii(span)}")
        print()
    total = sum(len(rows) for rows in found.values())
    print(f"TOTAL FINDINGS: {total}")
    if strict and total:
        print("strict: findings outstanding")
        return 1
    return 0


def run_gate(old_path: Path, new_path: Path) -> int:
    old, _ = load_master(old_path)
    new, _ = load_master(new_path)
    offenders, checked, atoms = new_facts(old, new)
    print("atom audit gate")
    print(f"  old: {_ascii(old_path)}")
    print(f"  new: {_ascii(new_path)}")
    if not offenders:
        print(f"no new facts: {checked} distinctive tokens across {atoms} atoms "
              f"all trace to the old file.")
        return 0
    print(f"NEW FACTS: {len(offenders)}")
    for where, kind, token in offenders:
        print(f"  {_ascii(where)}: {kind} {_ascii(token)}")
    print(f"FAIL: the new file states {len(offenders)} distinctive token(s) "
          f"the old file does not.")
    return 1


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atom_audit",
        description="Repeat / figure census and no-new-facts gate for the master atoms.")
    sub = parser.add_subparsers(dest="command", required=True)

    census = sub.add_parser("census", help="report repeats, figures and style in one master")
    census.add_argument("--file", type=Path, default=DEFAULT_MASTER,
                        help=f"master yaml to audit (default: {DEFAULT_MASTER})")
    census.add_argument("--strict", action="store_true",
                        help="exit 1 when anything is reported")

    slop = sub.add_parser(
        "slop", help="report AI-writing patterns in the atom fields, by severity tier")
    slop.add_argument("--file", type=Path, default=DEFAULT_MASTER,
                      help=f"master yaml to audit (default: {DEFAULT_MASTER})")
    slop.add_argument("--strict", action="store_true",
                      help="exit 1 when anything is reported")

    gate = sub.add_parser("gate", help="fail when the new file states a fact the old one does not")
    gate.add_argument("--old", type=Path, required=True, help="the pre-edit master")
    gate.add_argument("--new", type=Path, required=True, help="the candidate master")

    args = parser.parse_args(list(argv) if argv is not None else None)
    # A console that cannot encode a character should not take the report down.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        if args.command == "census":
            return run_census(args.file, args.strict)
        if args.command == "slop":
            return run_slop(args.file, args.strict)
        return run_gate(args.old, args.new)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"atom_audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
