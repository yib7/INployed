"""`docs/ARCHITECTURE.md`'s résumé-engine module table lists every module, and only real ones.

That table is the map a reader uses to find their way around `local/resume_tailor/`, and it
rots in both directions. A module added with no row is invisible: cycle 12 added
`itemcheck.py` and the table said nothing, and nothing failed. A row for a module that has
been deleted or renamed is worse, because it sends a reader looking for a file that is not
there; a prior cycle deleted parts of the tree that were documented and never real.

Both directions are checked here, as set equality. The alternative is a reviewer noticing,
and a reviewer noticed four gaps by hand once already.

The test reads the markdown as text. It deliberately does not import the package: a bad
import here would be a `load_dotenv()` at module scope away from placing a billed call, and
a table of file names has no business needing the code behind them.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "ARCHITECTURE.md"
PKG = REPO / "local" / "resume_tailor"

# The heading the table sits under, and the module cell's shape. A row may name several
# modules in one cell (the optional artifacts share one), so every `name.py` in the first
# cell counts.
_SECTION = "## The résumé engine in depth"
_MODULE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*\.py)`")


def _table_section() -> str:
    """The text from the résumé-engine heading to the next level-2 heading."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(_SECTION)
    rest = text[start + len(_SECTION):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def documented_modules() -> set:
    """Every module named in the first cell of a row of that section's table."""
    found = set()
    for line in _table_section().splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        if first_cell.strip().startswith("---") or not first_cell.strip():
            continue
        found.update(_MODULE.findall(first_cell))
    return found


def real_modules() -> set:
    """Every module in the package, minus the package marker (`__init__.py` re-exports and
    documents nothing, and a row for it would say only that Python needs it)."""
    return {p.name for p in PKG.glob("*.py") if p.name != "__init__.py"}


def test_the_table_names_every_module_in_the_package():
    missing = real_modules() - documented_modules()
    assert not missing, (
        "these modules exist with no row in the docs/ARCHITECTURE.md module table: "
        + ", ".join(sorted(missing))
        + ". Add a row saying what the module is for; a module nobody can find in the map "
          "is a module the next reader rewrites from scratch.")


def test_the_table_names_no_module_that_is_gone():
    stale = documented_modules() - real_modules()
    assert not stale, (
        "the docs/ARCHITECTURE.md module table has rows for modules that no longer exist: "
        + ", ".join(sorted(stale))
        + ". Delete or rename the row; a row pointing at a missing file sends a reader "
          "looking for code that is not there.")


def test_the_section_and_its_table_are_actually_found():
    """Guard against a vacuous pass.

    Both tests above compare a set built by parsing markdown. Rename the heading or reshape
    the table and that set is empty, at which case one of them still passes silently. So pin
    that the parse found a real table with the modules nobody is going to remove.
    """
    documented = documented_modules()
    assert len(documented) >= 20, documented
    assert {"run.py", "compose.py", "verify.py", "measure.py"} <= documented
