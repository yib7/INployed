"""A patch reaches the module that defines the name.

`apply_run` re-exports the names of the modules split out of it, for the
readers that reach them through it; `apply_fill` does the same for the
click API of `apply_click`, and the flow harness `apply_harness` for
`apply_pages`, `apply_flows` and `apply_invariants`. A monkeypatch on a re-export rebinds the
facade's name only, while the code reads the name from the module that
defines it, so the patch does nothing and the test passes for the wrong
reason. This test reads every test and script for patch targets on the
facades and asserts that each one is defined in the facade itself: a def, a
class, an assignment, an import of a module, or a name imported from a module
that was not split out of the facade (`jev`'s own `atomic_write_json`, which
`jev`'s code reads through its own binding).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FACADES = ("apply_run", "apply_fill", "jev", "apply_harness")
# Patched on the facade on purpose: the run and `apply_fill` call these through
# `apply_fill`'s own binding, so a patch there reaches the callers a test drives.
REEXPORT_PATCHES = {("apply_fill", "click"), ("apply_fill", "settle")}
# The modules split out of the facades: a name a facade imports from one of these
# is read by that module's code, so a patch on the facade's binding misses it.
SPLIT_OUT = {"apply_limits", "apply_outcome", "apply_sites", "apply_sendwatch", "apply_page",
             "apply_account_flow", "apply_route", "apply_gate", "apply_record", "apply_job",
             "apply_job_pages", "apply_job_form", "apply_job_submit", "apply_send_words",
             "apply_click", "apply_form_js", "jev_doubles", "apply_pages", "apply_flows",
             "apply_invariants"}
# Where a facade's source lives: `local/`, but the harness is in `tests/`.
HOME = {"apply_harness": REPO / "tests"}


def _defined(module: str) -> set[str]:
    path = HOME.get(module, REPO / "local") / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(st.name)
        elif isinstance(st, ast.Assign):
            names.update(t.id for t in st.targets if isinstance(t, ast.Name))
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            names.add(st.target.id)
        elif isinstance(st, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in st.names)
        elif isinstance(st, ast.ImportFrom) and st.module not in SPLIT_OUT:
            names.update(a.asname or a.name for a in st.names)
    return names


def _facade(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The facade module an expression names: `apply_run`, an alias of it, or
    an attribute chain ending in one (`apply_run.apply_fill`, `h.apply_run`)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr in FACADES:
        return node.attr
    return None


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _targets(path: Path) -> list[tuple[str, str, int]]:
    """(facade, name, line) for every patch target on a facade in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {m: m for m in FACADES}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in FACADES:
                    aliases[a.asname or a.name] = a.name
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if fn in ("setattr", "delattr", "object") and len(n.args) >= 2:
                mod, name = _facade(n.args[0], aliases), _str(n.args[1])
                if mod and name:
                    found.append((mod, name, n.lineno))
            if fn in ("setattr", "delattr", "patch") and n.args:
                dotted = _str(n.args[0])
                if dotted and "." in dotted:
                    mod, _, name = dotted.rpartition(".")
                    if mod.split(".")[-1] in FACADES:
                        found.append((mod.split(".")[-1], name, n.lineno))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and _facade(t.value, aliases):
                    found.append((_facade(t.value, aliases), t.attr, n.lineno))
        # ((module, name), value) pairs and {(module, name): value} keys
        # (`apply_pages.FAST_TIMING`, `Flow.timing`)
        pairs = []
        if isinstance(n, ast.Dict):
            pairs = [k for k in n.keys if k is not None]
        elif isinstance(n, ast.Tuple) and len(n.elts) == 2:
            pairs = [n.elts[0]]
        for k in pairs:
            if isinstance(k, ast.Tuple) and len(k.elts) == 2:
                mod, name = _str(k.elts[0]), _str(k.elts[1])
                if mod in FACADES and name:
                    found.append((mod, name, k.lineno))
    return found


def _all_targets() -> list[tuple[str, str, str, int]]:
    files = sorted((REPO / "tests").glob("*.py")) + sorted((REPO / "scripts").glob("*.py"))
    return [(f.name, mod, name, line) for f in files for mod, name, line in _targets(f)]


def test_the_scan_finds_the_patches_it_guards():
    found = {(mod, name) for _f, mod, name, _l in _all_targets()}
    # names patched today on each facade, so the scan cannot pass by finding nothing
    assert {("apply_run", "load_settings"), ("apply_run", "Runner"),
            ("apply_fill", "apply"), ("jev", "get"), ("apply_harness", "run_flow")} <= found


def test_every_patch_on_a_facade_targets_a_name_the_facade_defines():
    defined = {m: _defined(m) for m in FACADES}
    bad = [f"{f}:{line} patches {mod}.{name}, which {mod} only re-exports"
           for f, mod, name, line in _all_targets()
           if name not in defined[mod] and (mod, name) not in REEXPORT_PATCHES]
    assert not bad, "\n".join(bad)
