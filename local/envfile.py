"""Comment-preserving read/write for a `.env` file — tiny and dependency-free.

The config GUI edits secrets and paths that live in `.env`. This module updates
that file *surgically*: it changes only the keys you ask it to and leaves every
comment, blank line, key order, and unknown key exactly as the user wrote them.
Writes are atomic (temp file + `os.replace`) with a `.bak` backup, mirroring the
JSON config writer in settings.py.

Why not just use python-dotenv? It reads `.env` at runtime, but it has no
comment-preserving *writer*. We keep the dependency optional (used only for
loading at runtime) and own the small amount of write logic here so the user's
hand-written `.env` survives a Save.

Quoting on write is chosen for python-dotenv compatibility:
  * bare (unquoted) for simple tokens/ids,
  * single-quoted for values with spaces or backslashes (Windows paths) — dotenv
    treats single-quoted values literally, so `C:\\Users\\...` is never mangled,
  * double-quoted (with `\\` and `"` escaped) only when the value itself contains
    a single quote.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# KEY=VALUE line, tolerating leading whitespace and an optional `export `.
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# A value safe to write without any quoting (no spaces, quotes, #, or backslash).
_BARE_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:@,+=-]+$")

# Characters that cannot survive this format at all. Every value here becomes ONE
# `KEY=VALUE` line, and `read()` parses line by line, so a newline in a value does
# not round-trip -- it writes a second physical line that the reader then treats as
# a whole new assignment. Pasting a key that wrapped across two lines, or two pool
# keys on separate lines, would therefore invent a `.env` entry nobody typed. NUL
# and the other C0 controls cannot be represented either. Rejected rather than
# stripped: silently rewriting a credential produces a key that fails later with no
# clue why, which is worse than being told now.
#
# The class is exactly what `str.splitlines()` breaks on, and that is more
# than CR/LF: splitlines ALSO splits U+0085 NEL, U+2028 LINE SEPARATOR and
# U+2029 PARAGRAPH SEPARATOR, none of which falls inside \x00-\x1f\x7f.
# Without those three the guard was two thirds of a guard -- a value carrying
# U+2028 passed the check, was written as one quoted line, and came back out
# of read() as TWO assignments, which is the exact hole this exists to close.
# U+2028 and U+0085 are not exotic; they arrive in text pasted from a PDF or
# a web page.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _parse_value(raw: str) -> str:
    """Recover a value from the text right of `=`.

    Strips one matching pair of surrounding quotes; inside double quotes, undoes
    the `\\\\` / `\\"` escaping our writer applies. Unquoted values are taken
    verbatim (after trimming surrounding whitespace) — we deliberately do not
    parse inline `# comments`, since this project keeps comments on their own
    lines and that keeps round-tripping exact.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return raw


def read(path: str | os.PathLike) -> dict[str, str]:
    """Parse `path` into {KEY: value}; returns {} when the file is missing/unreadable.

    utf-8-sig so a BOM'd .env (Notepad, or PowerShell 5.1's `Set-Content
    -Encoding UTF8`) does not glue a U+FEFF onto the first line's key name,
    which makes _LINE_RE miss it and drops that key. Harmless only while .env
    starts with a comment -- exactly the kind of guarantee that quietly stops
    holding."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            out[m.group(1)] = _parse_value(m.group(2))
    return out


def _format_value(value: str) -> str:
    """Render a value for the right-hand side of `KEY=`, quoting as needed."""
    if value == "":
        return ""
    if _BARE_SAFE_RE.match(value):
        return value
    if "'" not in value:
        # Single quotes are literal in dotenv → safe for spaces and backslashes.
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, backing up any existing file to `.bak`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update(path: str | os.PathLike, updates: dict[str, str | None]) -> None:
    """Apply `updates` to the `.env` at `path`, preserving everything else.

    For each key: a string value sets/replaces it (in place if it already exists,
    else appended); a `None` value removes its line. Comments, blank lines, key
    order, and keys not in `updates` are left untouched. Written atomically with
    a `.bak` backup.

    Raises ValueError for a value carrying a newline or another control character
    (see `_CONTROL_RE`) -- checked for EVERY key before anything is written, so a
    rejected value cannot leave the file half-updated.
    """
    bad = sorted(k for k, v in updates.items()
                 if v is not None and _CONTROL_RE.search(str(v)))
    if bad:
        raise ValueError(
            f"Cannot write {', '.join(bad)} to {Path(path).name}: the value contains "
            "a line break or control character, and each setting has to fit on one "
            "line. Remove the line break (a key pasted from a wrapped line is the "
            "usual cause; a list of keys goes on one line, comma-separated).")
    p = Path(path)
    try:
        # utf-8-sig, matching read(): a BOM is stripped on the way in and not
        # written back, so an update also heals a BOM'd file rather than
        # carrying the marker forward into every later rewrite.
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, ValueError):
        lines = []

    remaining = dict(updates)  # keys we still need to place
    out_lines: list[str] = []
    for line in lines:
        m = _LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            new_val = remaining.pop(key)
            if new_val is None:
                continue  # drop the line (unset)
            out_lines.append(f"{key}={_format_value(new_val)}")
        else:
            out_lines.append(line)

    # Append any keys that weren't already present (skip removals of absent keys).
    appended = [f"{k}={_format_value(v)}" for k, v in remaining.items() if v is not None]
    if appended:
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.extend(appended)

    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write_text(p, text)
