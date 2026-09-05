# -*- coding: utf-8 -*-
"""Cross-platform text policy: explicit UTF-8 everywhere, and a line-ending rule.

Two things break a Windows-authored repo the first time it is cloned somewhere
else, and neither shows up as a test failure until a user hits it:

**1. The OS default encoding.** ``open()``, ``Path.read_text()`` and
``subprocess(text=True)`` with no ``encoding=`` decode with
``locale.getencoding()`` — cp1252 on this machine, UTF-8 on the ubuntu-latest CI
runner, cp932 on a Japanese Windows. A résumé bullet with an ``é``, a company
name with a curly apostrophe, or a pdflatex log echoing either one then comes out
mangled on one machine and raises ``UnicodeDecodeError`` on another. The repo has
been bitten by exactly this twice: ``compile.subprocess.run`` (pdflatex echoes the
document back) and ``vm_sync.run_cmd`` (gcloud echoes the error body) both carry a
comment about it, and ``local_task.register`` was found still doing it in cycle 11.
So the rule is: every text-mode read, write and subprocess pins ``encoding=``.

**2. Line endings.** ``.gitattributes`` settles what is stored and what is checked
out, which is what stops a Windows commit from arriving on Linux as CRLF. Two
extensions cannot be left to ``text=auto``: ``.sh`` is scp'd to the
Linux VM, where a CRLF shebang fails with a bare "not found"; ``.cmd`` and
``.ps1`` are the Windows launchers, and PowerShell 5.1 is only reliably parsed as
CRLF. A UTF-8 BOM is the third failure in this family — ``scripts/setup.ps1`` once
wrote ``local/config.json`` with one and ``json.loads`` rejected the file.

Everything here is a **static scan of source text**, parsed with ``ast``. Nothing
imports the project: ``local/resume_tailor/config.py`` calls ``load_dotenv()`` at
import scope, so importing it inside a test pulls live credentials into the
process. The file list comes from ``git ls-files`` so the scan can never open the
gitignored personal data (``.env``, the master CSV, ``master_experience.yaml``).

Running the suite as ``python -X warn_default_encoding -W error::EncodingWarning
-m pytest`` is the runtime counterpart to the first half, and agrees with it.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Text I/O whose encoding is the OS default when it is not named.
TEXT_IO = {"open", "read_text", "write_text"}
SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call"}

# `open` is not always file I/O. These receivers own a same-named method that
# takes no encoding at all, so a hit on them would be a false positive.
NON_FILE_OPEN_RECEIVERS = {"webbrowser", "os", "Image", "opener", "urllib",
                           "request", "shelve", "gzip", "zipfile", "tarfile"}

# pandas is deliberately out of scope: read_csv/to_csv document `utf-8` as their
# default and do not consult the locale, so naming it would be noise.


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                             capture_output=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pytest.skip("git is not available to enumerate tracked files")
    if out.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout")
    return [p for p in out.stdout.split("\0") if p]


# Positional index of `mode` and of `encoding` per call shape. Every one of these
# is routinely passed positionally in this tree -- `pdf_path.open("rb")` and
# `p.read_text("utf-8")` both appear -- so a keyword-only check would report the
# second as an offender and miss the first as an exemption.
_POSITIONS = {
    # name, is_method -> (mode index or None, encoding index or None)
    ("open", False): (1, 3),      # open(file, mode, buffering, encoding)
    ("open", True): (0, 2),       # Path.open(mode, buffering, encoding)
    ("read_text", True): (None, 0),   # Path.read_text(encoding, errors)
    ("write_text", True): (None, 1),  # Path.write_text(data, encoding, errors)
}


def _arg_at(call: ast.Call, idx):
    if idx is None or len(call.args) <= idx:
        return None
    node = call.args[idx]
    return node.value if isinstance(node, ast.Constant) else object()


def _mode_and_encoding(call: ast.Call, name: str, is_method: bool):
    mode_i, enc_i = _POSITIONS.get((name, is_method), (None, None))
    mode = _arg_at(call, mode_i)
    enc = _arg_at(call, enc_i)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
        elif kw.arg == "encoding":
            enc = kw.value
    return mode, enc


def _receiver(fn) -> str:
    if not isinstance(fn, ast.Attribute):
        return ""
    v = fn.value
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute):
        return v.attr
    return ""


def _scan(path: Path) -> list[str]:
    """Every call in `path` that decodes or encodes with the OS default."""
    hits: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_method = isinstance(fn, ast.Attribute)
        name = fn.attr if is_method else (fn.id if isinstance(fn, ast.Name) else None)
        if name is None:
            continue
        named = {k.arg for k in node.keywords}
        if name in TEXT_IO and _receiver(fn) not in NON_FILE_OPEN_RECEIVERS:
            mode, enc = _mode_and_encoding(node, name, is_method)
            if enc is None and "b" not in str(mode or ""):
                hits.append(f"line {node.lineno}: {name}(mode={mode!r}) has no encoding=")
        elif (name in SUBPROCESS_CALLS and "encoding" not in named
                and ("text" in named or "universal_newlines" in named)):
            hits.append(f"line {node.lineno}: subprocess {name}(text=...) has no encoding=")
    # ast.walk is breadth-first, so sort back into source order for readability.
    return sorted(hits, key=lambda h: int(h.split()[1].rstrip(":")))


def test_no_source_file_relies_on_the_os_default_encoding():
    offenders = {}
    scanned = 0
    for rel in _tracked_files():
        if not (rel.endswith(".py") or rel.endswith(".pyw")):
            continue
        p = REPO / rel
        if not p.exists():  # pragma: no cover - a deleted-but-staged path
            continue
        scanned += 1
        hits = _scan(p)
        if hits:
            offenders[rel] = hits
    assert scanned > 100, f"the scan found only {scanned} python files; it is not reaching the tree"
    assert not offenders, (
        "text I/O without an explicit encoding= decodes with the OS default "
        "(cp1252 here, UTF-8 on CI):\n"
        + "\n".join(f"  {f}\n    " + "\n    ".join(h) for f, h in sorted(offenders.items())))


def test_gitattributes_forces_line_endings_for_the_scripts_that_break_without_it():
    text = (REPO / ".gitattributes").read_text(encoding="utf-8")
    rules = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
    rules = [r for r in rules if r]

    def rule_for(pattern):
        return next((r for r in rules if r.split()[0] == pattern), None)

    assert any(r.startswith("* ") and "text=auto" in r for r in rules), \
        "the catch-all `* text=auto` is what normalizes everything else to LF in the repo"
    sh = rule_for("*.sh")
    assert sh and "eol=lf" in sh, \
        "run_scraper.sh runs on the Linux VM; a CRLF shebang fails with a bare 'not found'"
    for pattern in ("*.cmd", "*.ps1"):
        rule = rule_for(pattern)
        assert rule and "eol=crlf" in rule, \
            f"{pattern} is a Windows launcher and PowerShell 5.1 needs CRLF"


def test_no_tracked_text_file_carries_a_utf8_bom():
    binary_ext = {".png", ".gif", ".ico", ".jpg", ".jpeg", ".pdf", ".zip"}
    offenders = []
    for rel in _tracked_files():
        p = REPO / rel
        if p.suffix.lower() in binary_ext or not p.exists():
            continue
        with p.open("rb") as fh:
            if fh.read(3) == b"\xef\xbb\xbf":
                offenders.append(rel)
    assert not offenders, (
        "a UTF-8 BOM breaks json.loads, a shell shebang and PowerShell parsing: "
        f"{offenders}")


def test_ps1_scripts_are_pure_ascii():
    """PowerShell 5.1 mangles BOM-less non-ASCII, which corrupts parsing."""
    offenders = {}
    for rel in _tracked_files():
        if not rel.endswith(".ps1"):
            continue
        raw = (REPO / rel).read_bytes()
        bad = sorted({b for b in raw if b > 0x7F})
        if bad:
            offenders[rel] = [hex(b) for b in bad]
    assert not offenders, f"non-ASCII bytes in .ps1: {offenders}"


def test_this_files_own_scan_would_catch_a_regression(tmp_path):
    """A scan that can never fail is not a guard. Prove it fires."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('a').read_text()\n"
        "open('b', 'w').write('x')\n"
        "subprocess.run(['ls'], text=True)\n"
        "open('c', 'rb').read()\n"
        "Path('d').read_text(encoding='utf-8')\n"
        "Path('e').read_text('utf-8')\n"
        "Path('f').write_text('x', 'utf-8')\n"
        "Path('g').open('rb').read()\n"
        "subprocess.run(['ls'], text=True, encoding='utf-8')\n",
        encoding="utf-8")
    hits = _scan(bad)
    assert len(hits) == 3, hits
    assert "line 3" in hits[0] and "line 4" in hits[1] and "line 5" in hits[2]


def test_the_scan_ignores_binary_mode_and_non_file_open():
    """The two shapes that would otherwise flood the scan with false positives."""
    src = REPO / "local" / "resume_tailor" / "compile.py"
    if not src.exists():  # pragma: no cover
        pytest.skip("compile.py moved")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    opens = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "open"]
    assert opens, "compile.py should still contain pdf_path.open('rb')"
    assert not _scan(src), "compile.py pins utf-8 on every text call and opens the PDF in binary"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
