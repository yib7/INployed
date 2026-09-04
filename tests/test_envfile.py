"""Tests for local/envfile.py — the comment-preserving .env reader/writer.

The config GUI edits secrets/paths in .env. These tests pin the two properties
that matter: (1) reading recovers values (incl. quoted Windows paths), and
(2) writing updates only the targeted keys while preserving the user's comments,
blank lines, key order, and keys the schema doesn't know about — atomically and
with a .bak backup.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import envfile  # noqa: E402


def test_read_missing_file_returns_empty(tmp_path):
    assert envfile.read(tmp_path / "nope.env") == {}


def test_read_basic_pairs_ignoring_comments_and_blanks(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "FOO=bar\n"
        "\n"
        "  BAZ = qux  \n"
        "export EXPORTED=val\n",
        encoding="utf-8",
    )
    data = envfile.read(p)
    assert data == {"FOO": "bar", "BAZ": "qux", "EXPORTED": "val"}


def test_read_strips_matching_quotes_and_keeps_backslash_paths(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "DQ=\"hello world\"\n"
        "SQ='C:\\Program Files\\MiKTeX\\pdflatex.exe'\n",
        encoding="utf-8",
    )
    data = envfile.read(p)
    assert data["DQ"] == "hello world"
    assert data["SQ"] == "C:\\Program Files\\MiKTeX\\pdflatex.exe"


def test_update_replaces_value_in_place_preserving_everything_else(tmp_path):
    p = tmp_path / ".env"
    original = (
        "# header comment\n"
        "FOO=old\n"
        "# keep me\n"
        "UNKNOWN_KEY=leave-alone\n"
    )
    p.write_text(original, encoding="utf-8")
    envfile.update(p, {"FOO": "new"})
    text = p.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "# keep me" in text
    assert "UNKNOWN_KEY=leave-alone" in text
    assert envfile.read(p)["FOO"] == "new"
    # order preserved: FOO line still before UNKNOWN_KEY line
    assert text.index("FOO=") < text.index("UNKNOWN_KEY=")


def test_update_appends_new_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n", encoding="utf-8")
    envfile.update(p, {"NEWKEY": "value"})
    data = envfile.read(p)
    assert data["FOO"] == "bar"
    assert data["NEWKEY"] == "value"


def test_update_none_removes_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\nGONE=zap\n", encoding="utf-8")
    envfile.update(p, {"GONE": None})
    data = envfile.read(p)
    assert "GONE" not in data
    assert data["FOO"] == "bar"


def test_update_creates_bak_when_overwriting(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=old\n", encoding="utf-8")
    envfile.update(p, {"FOO": "new"})
    bak = p.with_name(p.name + ".bak")
    assert bak.exists()
    assert envfile.read(bak)["FOO"] == "old"


def test_update_quotes_spaces_and_backslashes_for_roundtrip(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    path_val = "C:\\Program Files\\Generated Resumes"
    envfile.update(p, {"OUT": path_val, "NAME": "Ada Lovelace"})
    # round-trips through our reader
    data = envfile.read(p)
    assert data["OUT"] == path_val
    assert data["NAME"] == "Ada Lovelace"
    # spaced/backslash values are single-quoted (dotenv-literal, no escape mangling)
    raw = p.read_text(encoding="utf-8")
    assert "OUT='C:\\Program Files\\Generated Resumes'" in raw


def test_update_writes_atomically_no_tmp_left(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n", encoding="utf-8")
    envfile.update(p, {"FOO": "baz"})
    leftovers = [q.name for q in tmp_path.iterdir() if ".tmp" in q.name]
    assert leftovers == []


def test_update_creates_file_when_absent(tmp_path):
    p = tmp_path / "fresh.env"
    envfile.update(p, {"FOO": "bar"})
    assert p.exists()
    assert envfile.read(p)["FOO"] == "bar"


def test_bare_safe_values_written_unquoted(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    envfile.update(p, {"TOKEN": "abc123-XYZ_tok.en"})
    raw = p.read_text(encoding="utf-8")
    assert "TOKEN=abc123-XYZ_tok.en" in raw


def test_read_recovers_the_first_key_from_a_bom_prefixed_env(tmp_path):
    """A BOM must not eat the first key.

    PowerShell 5.1's `Set-Content -Encoding UTF8` and Notepad both write a
    U+FEFF, which decoded as utf-8 glues itself onto the first line. When that
    line is `KEY=value`, _LINE_RE no longer matches and the key vanishes with no
    error. Today .env opens with a comment, so the damage was invisible; that is
    a property of the template, not a guarantee about the user's file.
    """
    p = tmp_path / ".env"
    # Raw bytes, not a U+FEFF string literal: a real BOM in this file's own
    # source is invisible to whoever reads the test next.
    p.write_bytes(b"\xef\xbb\xbf"
                  + b"BRIGHT_DATA_API_TOKEN=abc\nGEMINI_API_KEY=def\n")
    assert envfile.read(p) == {"BRIGHT_DATA_API_TOKEN": "abc",
                               "GEMINI_API_KEY": "def"}


def test_update_strips_a_bom_instead_of_carrying_it_forward(tmp_path):
    """An update heals a BOM'd .env rather than rewriting the marker."""
    p = tmp_path / ".env"
    p.write_bytes(b"\xef\xbb\xbf" + b"FOO=1\n# a comment\nBAR=2\n")
    envfile.update(p, {"BAR": "3"})
    assert p.read_bytes()[:3] != b"\xef\xbb\xbf"
    assert envfile.read(p) == {"FOO": "1", "BAR": "3"}
    assert "# a comment" in p.read_text(encoding="utf-8")


def test_update_refuses_a_value_carrying_a_newline(tmp_path):
    """A newline in a value does not round-trip through this format.

    Every value becomes ONE physical `KEY=VALUE` line and `read()` parses line by
    line, so writing "a\nGEMINI_API_KEY=stolen" would put a second assignment in
    the file that nobody typed — and the reader would honour it. The realistic way
    in is a paste: an API key copied out of a wrapped terminal line, or two pool
    keys copied on separate lines. Refused rather than stripped, because silently
    rewriting a credential yields a key that fails later with no clue why.
    """
    import pytest

    p = tmp_path / ".env"
    p.write_text("FOO=1\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        envfile.update(p, {"GEMINI_API_KEYS": "key1\nBRIGHT_DATA_API_TOKEN=injected"})
    assert "GEMINI_API_KEYS" in str(exc.value)
    # ...and the file is untouched: the check runs before anything is written.
    assert envfile.read(p) == {"FOO": "1"}


def test_update_rejects_every_bad_key_before_writing_any_good_one(tmp_path):
    """The refusal is all-or-nothing — a half-updated .env is the worse outcome."""
    import pytest

    p = tmp_path / ".env"
    p.write_text("FOO=1\nBAR=2\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        envfile.update(p, {"FOO": "9", "BAR": "ok\r\nEVIL=1", "BAZ": "x\x00y"})
    msg = str(exc.value)
    assert "BAR" in msg and "BAZ" in msg and "FOO" not in msg   # names only the offenders
    assert envfile.read(p) == {"FOO": "1", "BAR": "2"}          # nothing written


def test_update_still_accepts_the_awkward_but_legal_values(tmp_path):
    """The guard is control characters only: spaces, quotes and backslashes stay."""
    p = tmp_path / ".env"
    p.write_text("FOO=1\n", encoding="utf-8")
    envfile.update(p, {"WINPATH": r"C:\Program Files\MiKTeX\pdflatex.exe",
                       "QUOTED": "it's fine",
                       "POOL": "key1,key2,key3"})
    assert envfile.read(p) == {
        "FOO": "1",
        "WINPATH": r"C:\Program Files\MiKTeX\pdflatex.exe",
        "QUOTED": "it's fine",
        "POOL": "key1,key2,key3",
    }
