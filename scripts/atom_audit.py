"""Hygiene audit for `resume_tailor_files/master_experience.yaml`'s achievement atoms.

    python scripts/atom_audit.py census [--file PATH] [--strict]
    python scripts/atom_audit.py gate --old PATH --new PATH

`census` reports what makes tailored bullets repeat themselves or read as noise:
a phrase written into two fields of one atom (the tailor sees `what; how; scope;
impact...` as ONE line, so it gets the fact twice and prints it twice), a phrase
two atoms of the same entry both claim, every six-digit exact count, and any
`+`/`~` figure or smart-punctuation character.

`gate` is the no-new-facts guard for an agent-driven rewrite of those atoms: a
distinctive token (a number, or a word carrying a capital / internal case) that
the new file states and the old one does not is a FABRICATED fact, and the run
stops. It is the atom-layer twin of `local/resume_tailor/verify.py`, which gives
the same guarantee at the bullet layer; the tokenizing rules below are that
module's, re-stated rather than imported because importing anything under
`local/resume_tailor/` runs `config.load_dotenv()` at import scope and a stray
credential load has placed a billed API request before (see `.autopilot/AUTONOMY.md`).

Standard library plus `yaml`. Nothing under `local/` is imported, no file is
written, and no network call is made: the audit is read-only and free.
"""
from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal, InvalidOperation
from itertools import combinations, count
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Set, Tuple

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
        return run_gate(args.old, args.new)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"atom_audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
