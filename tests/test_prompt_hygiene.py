# -*- coding: utf-8 -*-
"""Prompt hygiene: a tailor prompt may not use the punctuation or the sentence
shapes it bans.

``compose.BANNED_PHRASING`` tells the model that em dashes and contrast framing
("X, not Y") are wrong, and ``compose._STYLE_BANS`` backs both with regexes. For a
long time the prompts carrying that instruction were themselves full of em dashes,
which is not merely embarrassing: the model copies what it is shown. Every copied
em dash makes ``compose.style_violations()`` non-empty, which buys a flash-tier
``enforce_style`` repair call and hands an otherwise-correct bullet to a rewriter
that can damage it. ``aiwriting.RULES_PROMPT`` already carried the fix
("Deliberately free of em dashes, since it is telling the model to avoid them");
this test carries it across the whole package.

Cycle 11 widened it from characters to the whole of ``_STYLE_BANS``. The same
argument covers both: a prompt that says "never write 'X, not Y'" while itself
saying "IGNORED, not followed" is teaching the construction it forbids. Six prose
sites were rewritten to close that (``common._PRINCIPLE``, ``common.fence_jd``,
``chat``'s no-JD note, two lines of ``compose.rephrase``'s system prompt, and
``coverletter``'s refine prompt).

**Scope: string literals that reach a model.** Comments and docstrings keep their
em dashes -- that is this repo's prose style and no model ever sees it. Comments
never appear in an AST at all, and docstrings are excluded explicitly, so a
blanket grep would be wrong where this test is right.

**The one exemption: the ban lists themselves.** ``compose.BANNED_PHRASING`` and
``aiwriting.RULES_PROMPT`` are enumerations -- they cannot forbid "holistic" or
"rather than" without quoting them. They are therefore skipped for the phrasing
patterns and only for those. The character bans still apply to them, because a
list of banned words never needs an em dash to name one.

The modules are read as SOURCE TEXT and parsed with ``ast``. Nothing here imports
``local.resume_tailor``: ``config.py`` calls ``load_dotenv()`` at import, so
importing the package inside a test would pull the developer's live credentials
into the process.

Only what the prompts actually ban is checked. En dashes and curly quotes are
deliberately NOT checked -- no prompt in this package forbids them, so banning
them here would enforce a rule the engine does not have.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

PKG = Path(__file__).resolve().parents[1] / "local" / "resume_tailor"

# What ``compose._STYLE_BANS``'s "em dash" pattern matches, as a literal check.
# (name, compiled pattern) -- keep in step with that regex. Checked in EVERY
# literal that reaches a model, the ban enumerations included.
BANNED_CHARS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("em dash", re.compile("—")),
    ("spaced double hyphen", re.compile(r"\s--\s")),
)

# The sentence shapes ``compose._STYLE_BANS`` rejects in a bullet, copied verbatim
# from it -- keep the two in step. Skipped inside the ENUMERATIONS below.
BANNED_PHRASINGS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("contrast framing",
     re.compile(r",\s*not\s|\bnot just\b|\brather than\b|\binstead of\b", re.I)),
    ("participial tail",
     re.compile(r",\s*(?:[a-z]+\s+){0,6}?(?:enabling|ensuring|allowing|driving"
                r"|resulting in|empowering|showcasing|highlighting|demonstrating)\b",
                re.I)),
    ("buzzword verb",
     re.compile(r"\b(?:leverag|utiliz|spearhead|harness|empower|streamlin"
                r"|supercharg|turbocharg|revolutioniz|democratiz)\w*", re.I)),
    ("hollow intensifier",
     re.compile(r"\b(?:seamless\w*|robust\w*|comprehensive|cutting-edge|innovative"
                r"|holistic|state-of-the-art|powerful\w*|world-class|best-in-class"
                r"|top-notch|groundbreaking|unparalleled|turnkey|blazing\w*"
                r"|lightning[- ]fast|game[- ]?chang\w*|revolutionar\w*|very"
                r"|successfully)\b", re.I)),
    ("vague quantifier",
     re.compile(r"\b(?:various|numerous|myriad|consistently|regularly)\b"
                r"|\ba (?:wide range|wide variety|plethora) of\b", re.I)),
)

BANNED: Tuple[Tuple[str, re.Pattern], ...] = BANNED_CHARS + BANNED_PHRASINGS

# Module-level constants that ENUMERATE the bans, so they must quote them. Only
# the phrasing patterns are waived here; the character bans still apply. Adding a
# name here waives a real check, so it takes a list that is a ban list.
ENUMERATIONS: Tuple[str, ...] = ("BANNED_PHRASING", "RULES_PROMPT")

# The package's one LLM entry point: ``llm.call(system, user, tier, ...)``.
# Reached bare (``from .llm import call``) and as an attribute
# (``compose.call(...)`` in coverletter.py), so match on the name either way.
LLM_ENTRY = "call"
PROMPT_POSITIONS = (0, 1)                 # call(system, user, ...)
PROMPT_KEYWORDS = ("system", "user")      # call(system=..., user=...)

# Prompt builders whose result reaches ``call()`` from OUTSIDE this package, so
# the call-site trace below cannot see them. ``chat.build_context()`` returns the
# entire system prompt; ``local/qt/chat_dialog.py`` is what hands it to
# ``chat.ask()``, and ``ask`` only ever sees it as a parameter. Add an entry here
# when you add another such builder.
EXTRA_PROMPT_ROOTS: Tuple[Tuple[str, str], ...] = (("chat.py", "build_context"),)

# Calls whose string arguments are patterns, not prose. ``style_violations()`` is
# genuinely reachable from a prompt (its RESULT, the violation names, rides in the
# enforce_style payload), and following it lands in ``_STYLE_BANS`` -- whose regex
# source contains a literal em dash that no model ever sees. Stop at the re call.
OPAQUE_CALLS: Dict[str, Set[str]] = {
    "re": {"compile", "search", "match", "fullmatch", "sub", "subn",
           "findall", "finditer", "split", "escape"},
}

_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# ── AST plumbing ─────────────────────────────────────────────────────────────
def _parse(pkg: Path) -> Dict[str, ast.Module]:
    return {p.name: ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for p in sorted(pkg.glob("*.py"))}


def _docstring_ids(tree: ast.Module) -> Set[int]:
    """Node ids of every docstring constant, so they can be skipped."""
    out: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _SCOPES) and ast.get_docstring(node, clean=False) is not None:
            out.add(id(node.body[0].value))          # type: ignore[attr-defined]
    return out


def _assignments(scope: ast.AST) -> Dict[str, List[ast.AST]]:
    """`name -> [value node]` for assignments made DIRECTLY in `scope`.

    Deliberately does not descend into nested defs: a name bound in an inner
    function is not the same binding as the outer one."""
    binds: Dict[str, List[ast.AST]] = {}

    def record(target: ast.AST, value: Optional[ast.AST]) -> None:
        if isinstance(target, ast.Name) and value is not None:
            binds.setdefault(target.id, []).append(value)

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Assign):
                for tgt in child.targets:
                    record(tgt, child.value)
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                record(child.target, child.value)
            if not isinstance(child, _SCOPES):
                visit(child)

    visit(scope)
    return binds


def _scope_map(tree: ast.Module) -> Dict[int, ast.AST]:
    """`id(node) -> nearest enclosing function (or the module)`."""
    out: Dict[int, ast.AST] = {id(tree): tree}

    def visit(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = scope
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            visit(child, inner)

    visit(tree, tree)
    return out


def _called_name(node: ast.Call) -> Optional[str]:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _is_opaque(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    return (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
            and fn.attr in OPAQUE_CALLS.get(fn.value.id, ()))


def _walk_pruned(node: ast.AST) -> Iterable[ast.AST]:
    """``ast.walk`` that does not descend into opaque calls.

    ``ast.walk`` queues a node's children before the caller can reject it, so
    skipping the ``re.compile(...)`` node itself would still visit its pattern
    string. Prune the subtree instead."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack += [c for c in ast.iter_child_nodes(cur) if not _is_opaque(c)]


class _Index:
    """Everything the trace needs about one package, parsed from source only."""

    def __init__(self, pkg: Path):
        self.trees = _parse(pkg)
        self.sources = {p.name: p.read_text(encoding="utf-8").splitlines()
                        for p in sorted(pkg.glob("*.py"))}
        self.docstrings = {m: _docstring_ids(t) for m, t in self.trees.items()}
        self.scopes = {m: _scope_map(t) for m, t in self.trees.items()}
        # Per-module, per-scope local bindings.
        self.locals: Dict[str, Dict[int, Dict[str, List[ast.AST]]]] = {}
        # Package-wide module-level constants and defs, resolved by bare name:
        # `_PRINCIPLE` and `fence_jd` are defined in common.py but used in
        # compose.py, and `from .common import ...` erases the module prefix.
        self.globals: Dict[str, Tuple[str, ast.AST]] = {}
        self.funcs: Dict[str, Tuple[str, ast.AST]] = {}
        # Constant nodes living inside a ban ENUMERATION, which is allowed to
        # quote the phrasings it forbids (but not the characters).
        self.enumerations: Dict[str, Set[int]] = {}
        for mod, tree in self.trees.items():
            enum_ids: Set[int] = set()
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id in ENUMERATIONS
                        for t in node.targets):
                    enum_ids |= {id(c) for c in ast.walk(node.value)
                                 if isinstance(c, ast.Constant)}
            self.enumerations[mod] = enum_ids
            per_scope = {id(tree): _assignments(tree)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    per_scope[id(node)] = _assignments(node)
            self.locals[mod] = per_scope
            for name, vals in per_scope[id(tree)].items():
                self.globals.setdefault(name, (mod, vals[0]))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.funcs.setdefault(node.name, (mod, node))

    def scope_locals(self, mod: str, scope: ast.AST) -> Dict[str, List[ast.AST]]:
        return self.locals.get(mod, {}).get(id(scope), {})


# ── the trace: from a call() argument to every literal that can reach it ─────
def _prompt_roots(index: _Index) -> List[Tuple[str, ast.AST, ast.AST]]:
    """`(module, scope, node)` for every expression handed to the model."""
    roots: List[Tuple[str, ast.AST, ast.AST]] = []
    for mod, tree in index.trees.items():
        scopes = index.scopes[mod]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) != LLM_ENTRY:
                continue
            scope = scopes.get(id(node), tree)
            args = [node.args[i] for i in PROMPT_POSITIONS if i < len(node.args)]
            args += [kw.value for kw in node.keywords if kw.arg in PROMPT_KEYWORDS]
            roots += [(mod, scope, a) for a in args]
    for mod, func in EXTRA_PROMPT_ROOTS:
        tree = index.trees.get(mod)
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                roots += [(mod, node, r.value) for r in ast.walk(node)
                          if isinstance(r, ast.Return) and r.value is not None]
    return roots


def _reachable_constants(index: _Index) -> List[Tuple[str, ast.Constant]]:
    """Every string literal that can end up inside a prompt, with its module.

    Walks each root expression; a bare ``Name`` is resolved against the enclosing
    function's locals, then the module's globals, then the package's (which is how
    ``_PRINCIPLE`` and ``BANNED_PHRASING`` get pulled in), and a ``Call`` to a
    package helper is followed into that helper's ``return`` expressions (which is
    how ``common.fence_jd``'s untrusted-JD banner gets pulled in). An ``Attribute``
    on a sibling module resolves against that module's own module-level bindings,
    which is how ``aiwriting.RULES_PROMPT`` -- appended to three cover-letter
    prompts and invisible to a bare-name lookup -- gets pulled in."""
    found: Dict[Tuple[str, int], Tuple[str, ast.Constant]] = {}
    seen: Set[Tuple[str, int]] = set()
    work = list(_prompt_roots(index))
    while work:
        mod, scope, node = work.pop()
        key = (mod, id(node))
        if key in seen:
            continue
        seen.add(key)
        if _is_opaque(node):
            continue
        for inner in _walk_pruned(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                if id(inner) not in index.docstrings.get(mod, ()):
                    found.setdefault((mod, id(inner)), (mod, inner))
            elif isinstance(inner, ast.Name):
                vals = index.scope_locals(mod, scope).get(inner.id)
                if vals:
                    work += [(mod, scope, v) for v in vals]
                    continue
                glob = index.globals.get(inner.id)
                if glob:
                    gmod, gval = glob
                    work.append((gmod, index.trees[gmod], gval))
                    continue
                fn = index.funcs.get(inner.id)
                if fn:
                    fmod, fnode = fn
                    work += [(fmod, fnode, r.value) for r in ast.walk(fnode)
                             if isinstance(r, ast.Return) and r.value is not None]
            elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                sib = f"{inner.value.id}.py"
                stree = index.trees.get(sib)
                if stree is not None:
                    work += [(sib, stree, v) for v in
                             index.scope_locals(sib, stree).get(inner.attr, [])]
    return list(found.values())


def _source_lines(index: _Index, mod: str, node: ast.Constant,
                  pattern: re.Pattern) -> List[int]:
    """Source line of each match, in order, inside `node`'s exact source span.

    ``node.lineno`` alone is not enough: adjacent literals fold into ONE
    ``Constant`` spanning 20 lines of prompt, and an f-string's chunks share
    their boundary line with the neighbouring chunk. So slice the source by
    (lineno, col_offset)..(end_lineno, end_col_offset) and map each match's
    offset back to a line. Returns [] when the span holds no match (an escape
    sequence, say), and the caller falls back to the literal's first line."""
    lines = index.sources.get(mod, [])
    start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
    if not lines or end > len(lines):
        return []
    chunk = lines[start - 1:end]
    chunk[-1] = chunk[-1][:getattr(node, "end_col_offset", len(chunk[-1]))]
    pad = node.col_offset                       # keep offsets aligned on line 1
    chunk[0] = " " * pad + chunk[0][pad:] if len(chunk) > 1 else chunk[0][pad:]
    text = "\n".join(chunk)
    starts, pos = [], 0
    for line in chunk:
        starts.append(pos)
        pos += len(line) + 1
    out = []
    for match in pattern.finditer(text):
        idx = max(i for i, s in enumerate(starts) if s <= match.start())
        out.append(start + idx)
    return out


def scan(pkg: Path = PKG) -> List[Dict[str, Any]]:
    """Banned characters and phrasings in literals that reach a model. Empty = clean."""
    index = _Index(pkg)
    out: List[Dict[str, Any]] = []
    for mod, node in _reachable_constants(index):
        in_enum = id(node) in index.enumerations.get(mod, ())
        for label, pattern in (BANNED_CHARS if in_enum else BANNED):
            matches = list(pattern.finditer(node.value))
            at = _source_lines(index, mod, node, pattern)
            if len(at) != len(matches):          # escapes: attribution unreliable
                at = [node.lineno] * len(matches)
            for line, match in zip(at, matches):
                lo = max(0, match.start() - 45)
                snippet = node.value[lo:match.end() + 45].replace("\n", " ")
                out.append({"module": mod, "line": line, "ban": label,
                            "snippet": snippet.strip()})
    return sorted(out, key=lambda h: (h["module"], h["line"], h["ban"], h["snippet"]))


def _report(hits: List[Dict[str, Any]]) -> str:
    head = (f"{len(hits)} banned character(s)/phrasing(s) in prompt text sent to the "
            "model.\nThese prompts tell the model not to use them, so every one here "
            "is an example the model will copy -- and a copied em dash or 'X, not Y' "
            "costs an enforce_style repair call on a bullet that was already correct. "
            "Rewrite it: state the positive claim once, and reach for a comma, colon, "
            "parenthesis or sentence break instead of a dash (comments and docstrings "
            "are exempt and are not checked; the ban lists in ENUMERATIONS are exempt "
            "from the phrasing patterns only).\n")
    body = "\n".join(f"  local/resume_tailor/{h['module']}:{h['line']}: "
                     f"{h['ban']} -> ...{h['snippet']}..." for h in hits)
    return head + body


# ── tests ────────────────────────────────────────────────────────────────────
def test_prompts_are_free_of_the_characters_they_ban():
    hits = scan()
    assert not hits, _report(hits)


def test_the_trace_still_reaches_every_prompt_module():
    """Guard against a vacuous pass.

    The scan finds prompts by looking for ``call(...)``. Rename or wrap that entry
    point and the scan silently inspects nothing while still reporting green. Pin
    the modules whose prompts must stay in view."""
    index = _Index(PKG)
    reached = {mod for mod, _ in _reachable_constants(index)}
    expected = {"aiwriting.py", "chat.py", "common.py", "compose.py", "coverletter.py",
                "master_gaps.py", "prep.py", "research.py", "selection.py",
                "skills.py"}
    assert expected <= reached, (
        "the prompt trace no longer reaches: " + ", ".join(sorted(expected - reached))
        + " -- llm.call was probably renamed or wrapped; update LLM_ENTRY.")


def _fake_pkg(tmp_path: Path) -> Path:
    """A miniature package exercising each way a literal reaches a prompt."""
    pkg = tmp_path / "fake_tailor"
    pkg.mkdir()
    (pkg / "shared.py").write_text(
        '"""A docstring with an em dash — which must be ignored."""\n'
        'PRINCIPLE = "shared rule — imported into another module"\n'
        'BANNED_PHRASING = "never write \'X, not Y\' — and never dash"\n'
        '\n'
        'def banner(text):\n'
        '    """Docstring — ignored."""\n'
        '    # Comment — ignored.\n'
        '    return "BANNER — from a helper: " + text\n',
        encoding="utf-8")
    (pkg / "writer.py").write_text(
        'from . import shared\n'
        'from .shared import PRINCIPLE, banner\n'
        'from .llm import call\n'
        '\n'
        'CLEAN = "no banned punctuation here"\n'
        '\n'
        'def go(jd):\n'
        '    system = "inline literal — straight in the call" + PRINCIPLE\n'
        '    user = f"{banner(jd)}\\n{CLEAN}\\n{shared.BANNED_PHRASING}"\n'
        '    return call(system, user, "tier")\n'
        '\n'
        'def unused():\n'
        '    return "never reaches a model — so never flagged"\n',
        encoding="utf-8")
    return pkg


def test_scan_detects_every_route_a_literal_takes_into_a_prompt(tmp_path):
    hits = scan(_fake_pkg(tmp_path))
    by_module = {(h["module"], h["line"], h["ban"]) for h in hits}
    assert ("writer.py", 8, "em dash") in by_module, "inline literal in the call argument"
    assert ("shared.py", 2, "em dash") in by_module, "module constant imported across modules"
    assert ("shared.py", 8, "em dash") in by_module, "return value of a helper in the prompt"
    assert ("shared.py", 3, "em dash") in by_module, (
        "a sibling module's constant reached by attribute (aiwriting.RULES_PROMPT's route)")
    assert len(hits) == 4, ("docstrings, comments and unreachable functions must "
                            f"not be flagged; got {hits}")


def test_ban_enumerations_may_quote_the_phrasings_they_forbid(tmp_path):
    """``BANNED_PHRASING`` cannot say "never write 'X, not Y'" without writing it.

    The waiver is narrow on purpose: the enumeration is skipped for the PHRASING
    patterns and for nothing else, so the em dash on the same line is still a hit
    (asserted above). Without that split the gate would either fail forever on its
    own ban list or stop policing the ban list's punctuation."""
    hits = scan(_fake_pkg(tmp_path))
    assert not [h for h in hits if h["ban"] == "contrast framing"], (
        "the enumeration's quoted 'X, not Y' must not be flagged as a violation; "
        f"got {hits}")


def test_a_phrasing_ban_is_enforced_outside_an_enumeration(tmp_path):
    """Guard against a vacuous exemption: the phrasing patterns must still bite."""
    pkg = _fake_pkg(tmp_path)
    (pkg / "writer.py").write_text(
        (pkg / "writer.py").read_text(encoding="utf-8").replace(
            'CLEAN = "no banned punctuation here"',
            'CLEAN = "say so rather than guessing what the role involves"'),
        encoding="utf-8")
    hits = scan(pkg)
    assert ("writer.py", 5, "contrast framing") in {
        (h["module"], h["line"], h["ban"]) for h in hits}, hits
